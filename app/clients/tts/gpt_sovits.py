"""GPT-SoVITS API v2 adapter with bounded, atomic WAV handling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import ssl
import time
import wave
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import uuid4

import httpx

from app.core.cancellation import CancellationToken
from app.provider_transport import (
    EndpointKind,
    build_ssl_context,
    validate_endpoint,
    validate_proxy_url,
)
from app.schemas import AudioResult, TTSJob
from app.temp_assets import TempAssetKind, TempAssetRegistry, TempRegistryError

_T = TypeVar("_T")
_WAVE_CONTENT_TYPES = frozenset({"audio/wav", "audio/wave", "audio/x-wav"})
_CACHE_PARTIAL_MAX_AGE_SECONDS = 300.0
_SENSITIVE_TEXT = re.compile(
    r"(?i)(password|passcode|api[-_ ]?key|access[-_ ]?token|secret|"
    r"验证码|密码|口令|身份证|银行卡|信用卡|手机号|住址|病历)|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)|\d{12,19}"
)


@dataclass(frozen=True, slots=True)
class GPTSoVITSPreset:
    """Voice-affecting parameters accepted by GPT-SoVITS ``/tts``."""

    ref_audio_path: str
    ref_audio_scope: Literal["service_resource", "local_file"] = "service_resource"
    prompt_text: str = ""
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    top_k: int = 5
    top_p: float = 1.0
    temperature: float = 1.0
    text_split_method: str = "cut5"
    batch_size: int = 1
    batch_threshold: float = 0.75
    split_bucket: bool = True
    speed_factor: float = 1.0
    fragment_interval: float = 0.3
    seed: int = -1
    parallel_infer: bool = True
    repetition_penalty: float = 1.35

    def __post_init__(self) -> None:
        if not self.ref_audio_path.strip():
            raise ValueError("ref_audio_path 不能为空")
        if self.ref_audio_scope not in {"service_resource", "local_file"}:
            raise ValueError("ref_audio_scope 无效")
        if self.top_k < 1 or self.batch_size < 1:
            raise ValueError("top_k 和 batch_size 必须大于 0")
        if not 0.0 < self.top_p <= 1.0 or self.temperature <= 0.0:
            raise ValueError("top_p/temperature 超出有效范围")
        if self.speed_factor <= 0.0 or self.fragment_interval < 0.0:
            raise ValueError("speed_factor 必须为正且 fragment_interval 不能为负")


@dataclass(frozen=True, slots=True)
class GPTSoVITSProbe:
    available: bool
    protocol: str | None
    status_code: int | None = None
    error_code: str | None = None


class _AudioValidationError(ValueError):
    pass


class _AudioTooLargeError(ValueError):
    pass


class _FirstByteTimeoutError(TimeoutError):
    pass


@dataclass(slots=True)
class _KeyLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class GPTSoVITSProvider:
    """Synthesize segments with an opt-in, privacy-aware persistent WAV cache."""

    def __init__(
        self,
        base_url: str,
        output_directory: Path,
        presets: Mapping[str, GPTSoVITSPreset],
        *,
        default_preset: str = "default",
        timeout_seconds: float | None = None,
        max_audio_bytes: int = 32 * 1024 * 1024,
        cache_enabled: bool = False,
        cache_dir: Path | None = None,
        cache_max_bytes: int = 512 * 1024 * 1024,
        cache_ttl_seconds: float = 7 * 24 * 60 * 60,
        sensitive_text_predicate: Callable[[str], bool] | None = None,
        proxy_url: str | None = None,
        ca_bundle_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        temp_registry: TempAssetRegistry | None = None,
    ) -> None:
        if timeout_seconds is not None:
            raise ValueError("GPT-SoVITS constructor timeout is forbidden; TTSJob owns deadlines")
        if max_audio_bytes < 44:
            raise ValueError("max_audio_bytes 必须至少容纳 WAV header")
        if default_preset not in presets:
            raise ValueError("default_preset 必须存在于 presets")
        if cache_enabled and cache_dir is None:
            raise ValueError("启用持久缓存时必须配置 cache_dir")
        if cache_max_bytes < 1 or cache_ttl_seconds <= 0.0:
            raise ValueError("cache_max_bytes/cache_ttl_seconds 必须大于 0")
        validate_endpoint(base_url, kind=EndpointKind.http)
        if client is not None and transport is not None:
            raise ValueError("GPT-SoVITS test transport must have one owner")
        if proxy_url is not None:
            validate_proxy_url(proxy_url)
        self._output_directory = output_directory
        self._presets = dict(presets)
        self._default_preset = default_preset
        self._max_audio_bytes = max_audio_bytes
        self._cache_enabled = cache_enabled
        self._cache_dir = cache_dir
        self._cache_max_bytes = cache_max_bytes
        self._cache_ttl_seconds = cache_ttl_seconds
        self._sensitive_text_predicate = sensitive_text_predicate
        self._temp_registry = temp_registry
        self._tts_endpoint = httpx.URL(base_url.rstrip("/") + "/").join("tts")
        for preset in presets.values():
            _validate_reference_scope(self._tts_endpoint, preset)
        if client is not None:
            transport = client._transport
        self._client = httpx.AsyncClient(
            timeout=None,
            verify=build_ssl_context(ca_bundle_path),
            proxy=proxy_url,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._paths: set[Path] = set()
        self._asset_ids: dict[Path, str] = {}
        self._cache_results: dict[str, Path] = {}
        self._key_locks: dict[str, _KeyLock] = {}
        self._cache_maintenance_lock = asyncio.Lock()
        self._synthesis_tasks: set[asyncio.Task[AudioResult]] = set()
        self._synthesis_cancellations: set[asyncio.Task[AudioResult]] = set()
        self._synthesis_calls: set[asyncio.Future[None]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    async def probe(self, *, timeout_ms: int) -> GPTSoVITSProbe:
        """Identify an API v2 ``/tts`` route without generating or saving audio."""

        if timeout_ms <= 0:
            raise ValueError("GPT-SoVITS probe timeout must be positive")
        if self._closed:
            return GPTSoVITSProbe(False, None, error_code="tts_closed")
        response: httpx.Response | None = None
        try:
            request = self._client.build_request("POST", self._tts_endpoint, json={})
            request.extensions["timeout"] = {
                "connect": timeout_ms / 1000,
                "read": timeout_ms / 1000,
                "write": timeout_ms / 1000,
                "pool": timeout_ms / 1000,
            }
            response = await asyncio.wait_for(
                self._client.send(request, stream=True),
                timeout=timeout_ms / 1000,
            )
        except (TimeoutError, httpx.TimeoutException):
            return GPTSoVITSProbe(False, None, error_code="tts_timeout")
        except httpx.RequestError:
            return GPTSoVITSProbe(False, None, error_code="tts_unavailable")
        try:
            if response.status_code in {200, 400, 405, 422}:
                return GPTSoVITSProbe(True, "api_v2", status_code=response.status_code)
            return GPTSoVITSProbe(
                False,
                None,
                status_code=response.status_code,
                error_code=(
                    "tts_unavailable" if response.status_code >= 500 else "tts_protocol_error"
                ),
            )
        finally:
            await response.aclose()

    async def synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult:
        token.raise_if_cancelled()
        if self._closed:
            return self._failure(job, "tts_closed")

        # Register the worker and its public call completion without yielding. A
        # concurrently scheduled close therefore either sees this operation or
        # flips ``_closed`` first and makes the call fail closed above.
        call_done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        worker = asyncio.create_task(
            self._synthesize(job, segment_index=segment_index, token=token),
            name=f"gpt-sovits-synthesis-{job.job_id}",
        )
        self._synthesis_calls.add(call_done)
        self._synthesis_tasks.add(worker)
        worker.add_done_callback(self._synthesis_finished)
        try:
            return await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=job.timeout_ms / 1000,
            )
        except TimeoutError:
            self._cancel_synthesis(worker)
            settled = await _join_task(worker, job.cancellation_timeout_ms / 1000)
            return self._failure(
                job,
                "tts_total_timeout" if settled else "tts_cancel_timeout",
            )
        except asyncio.CancelledError:
            self._cancel_synthesis(worker)
            await _join_task(worker, job.cancellation_timeout_ms / 1000)
            raise
        finally:
            self._synthesis_calls.discard(call_done)
            if not call_done.done():
                call_done.set_result(None)

    async def _synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult:
        token.raise_if_cancelled()

        preset = self._presets.get(job.style, self._presets[self._default_preset])
        request_payload = asdict(preset)
        request_payload.pop("ref_audio_scope")
        request_payload.update(
            {
                "text": job.text,
                "speed_factor": preset.speed_factor * job.speed_factor,
                "media_type": "wav",
                "streaming_mode": False,
            }
        )
        cache_key = self._cache_key(job.text, request_payload)
        if cache_key is None:
            return await self._synthesize_uncached(job, segment_index, token, request_payload)

        key_lock = await self._acquire_key_lock(cache_key, token)
        try:
            cached = await self._load_cached_result(job, cache_key, token)
            if cached is not None:
                return cached
            result = await self._synthesize_uncached(job, segment_index, token, request_payload)
            if not result.success:
                return result
            return await self._promote_to_cache(result, cache_key, token)
        finally:
            self._release_key_lock(cache_key, key_lock)

    async def _synthesize_uncached(
        self,
        job: TTSJob,
        segment_index: int,
        token: CancellationToken,
        request_payload: Mapping[str, object],
    ) -> AudioResult:
        final_path = self._result_path(job, segment_index)
        part_path = final_path.with_name(f".{final_path.name}.{uuid4().hex}.part")
        response: httpx.Response | None = None
        replace_started = False
        keep_final = False
        try:
            await _run_to_thread(self._register_temp_path, part_path, TempAssetKind.tts_part)
            request = self._client.build_request("POST", self._tts_endpoint, json=request_payload)
            request.extensions["timeout"] = {
                "connect": job.connect_timeout_ms / 1000,
                "read": None,
                "write": None,
                "pool": job.connect_timeout_ms / 1000,
            }
            response = await _await_with_token(self._client.send(request, stream=True), token)
            error_code = self._response_error(response)
            if error_code is not None:
                return self._failure(job, error_code)

            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in _WAVE_CONTENT_TYPES:
                return self._failure(job, "tts_invalid_content_type")
            declared_size = _content_length(response)
            if declared_size is not None and declared_size > self._max_audio_bytes:
                return self._failure(job, "tts_response_too_large")

            final_path.parent.mkdir(parents=True, exist_ok=True)
            await self._write_response(
                response,
                part_path,
                token,
                first_byte_timeout_seconds=job.first_byte_timeout_ms / 1000,
            )
            sample_rate, duration_ms = await _run_to_thread(_inspect_wave, part_path)
            token.raise_if_cancelled()
            replace_started = True
            await _run_to_thread(os.replace, part_path, final_path)
            await _run_to_thread(
                self._move_temp_path,
                part_path,
                final_path,
                TempAssetKind.tts_wav,
            )
            self._paths.add(final_path)
            if token.cancelled:
                await self._discard_path(final_path)
                token.raise_if_cancelled()
            result = AudioResult(
                job_id=job.job_id,
                turn_id=job.turn_id,
                segment_id=job.segment_id,
                success=True,
                audio_path=final_path,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
            )
            keep_final = True
            return result
        except _AudioTooLargeError:
            return self._failure(job, "tts_response_too_large")
        except _AudioValidationError:
            return self._failure(job, "tts_invalid_audio")
        except _FirstByteTimeoutError:
            return self._failure(job, "tts_first_byte_timeout")
        except httpx.ConnectTimeout:
            return self._failure(job, "tts_connect_timeout")
        except httpx.TimeoutException:
            return self._failure(job, "tts_total_timeout")
        except httpx.RequestError as exc:
            return self._failure(
                job,
                "tts_tls_error" if _has_ssl_error(exc) else "tts_connection_error",
            )
        except TempRegistryError:
            return self._failure(job, "tts_temp_registry_failed")
        finally:
            await _finish_cleanup(
                self._cleanup_uncached(
                    response,
                    part_path,
                    final_path if replace_started and not keep_final else None,
                )
            )

    async def _cleanup_uncached(
        self,
        response: httpx.Response | None,
        part_path: Path,
        incomplete_final_path: Path | None,
    ) -> None:
        if response is not None:
            await response.aclose()
        await self._delete_temp_path(part_path)
        if incomplete_final_path is not None:
            self._paths.discard(incomplete_final_path)
            await self._delete_temp_path(incomplete_final_path)
        await _run_to_thread(_remove_empty_parent, part_path.parent)

    async def _write_response(
        self,
        response: httpx.Response,
        path: Path,
        token: CancellationToken,
        *,
        first_byte_timeout_seconds: float,
    ) -> None:
        byte_count = 0
        stream = response.aiter_bytes()
        first_byte_deadline = asyncio.get_running_loop().time() + first_byte_timeout_seconds
        received_first_byte = False
        with path.open("xb") as output:
            while True:
                try:
                    if received_first_byte:
                        chunk = await _await_with_token(anext(stream), token)
                    else:
                        remaining = first_byte_deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise _FirstByteTimeoutError
                        try:
                            chunk = await asyncio.wait_for(
                                _await_with_token(anext(stream), token),
                                timeout=remaining,
                            )
                        except TimeoutError as exc:
                            raise _FirstByteTimeoutError from exc
                except StopAsyncIteration:
                    break
                if not chunk:
                    continue
                received_first_byte = True
                byte_count += len(chunk)
                if byte_count > self._max_audio_bytes:
                    raise _AudioTooLargeError
                output.write(chunk)
        if byte_count == 0:
            raise _AudioValidationError

    def _cache_key(self, text: str, request_payload: Mapping[str, object]) -> str | None:
        if not self._cache_enabled or self._cache_dir is None:
            return None
        if _SENSITIVE_TEXT.search(text):
            return None
        if self._sensitive_text_predicate is not None:
            try:
                if self._sensitive_text_predicate(text):
                    return None
            except Exception:
                return None
        serialized = json.dumps(
            {
                "endpoint": str(self._tts_endpoint),
                "request": request_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    async def _acquire_key_lock(self, cache_key: str, token: CancellationToken) -> _KeyLock:
        entry = self._key_locks.setdefault(cache_key, _KeyLock())
        entry.users += 1
        try:
            await _await_with_token(entry.lock.acquire(), token)
        except BaseException:
            entry.users -= 1
            if entry.users == 0 and self._key_locks.get(cache_key) is entry:
                self._key_locks.pop(cache_key, None)
            raise
        return entry

    def _release_key_lock(self, cache_key: str, entry: _KeyLock) -> None:
        entry.lock.release()
        entry.users -= 1
        if entry.users == 0 and self._key_locks.get(cache_key) is entry:
            self._key_locks.pop(cache_key, None)

    async def _load_cached_result(
        self,
        job: TTSJob,
        cache_key: str,
        token: CancellationToken,
    ) -> AudioResult | None:
        assert self._cache_dir is not None
        path = self._cache_dir / f"{cache_key}.wav"
        async with self._cache_maintenance_lock:
            token.raise_if_cancelled()
            try:
                if await _run_to_thread(path.is_symlink):
                    await _run_to_thread(_safe_unlink, path)
                    return None
                file_stat = await _run_to_thread(path.stat)
                if file_stat.st_size > min(self._max_audio_bytes, self._cache_max_bytes):
                    await _run_to_thread(_safe_unlink, path)
                    return None
                if time.time() - file_stat.st_mtime > self._cache_ttl_seconds:
                    await _run_to_thread(_safe_unlink, path)
                    return None
                sample_rate, duration_ms = await _run_to_thread(_inspect_wave, path)
                await _run_to_thread(os.utime, path, None)
            except (FileNotFoundError, OSError, _AudioValidationError):
                await _run_to_thread(_safe_unlink, path)
                return None
            token.raise_if_cancelled()
            result = AudioResult(
                job_id=job.job_id,
                turn_id=job.turn_id,
                segment_id=job.segment_id,
                success=True,
                audio_path=path,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
            )
            self._cache_results[result.audio_id] = path
            try:
                await self._cleanup_cache_locked()
            except BaseException:
                self._cache_results.pop(result.audio_id, None)
                raise
            return result

    async def _promote_to_cache(
        self,
        result: AudioResult,
        cache_key: str,
        token: CancellationToken,
    ) -> AudioResult:
        assert self._cache_dir is not None
        assert result.audio_path is not None
        source = result.audio_path
        cached_result: AudioResult | None = None
        cache_path: Path | None = None
        cache_part: Path | None = None
        replaced = False
        try:
            source_size = (await _run_to_thread(source.stat)).st_size
            if source_size > self._cache_max_bytes:
                return result
            cache_path = self._cache_dir / f"{cache_key}.wav"
            cache_part = cache_path.with_name(f".{cache_path.name}.{uuid4().hex}.part")
            async with self._cache_maintenance_lock:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    token.raise_if_cancelled()
                    await _run_to_thread(shutil.copyfile, source, cache_part)
                    token.raise_if_cancelled()
                    await _run_to_thread(os.replace, cache_part, cache_path)
                    replaced = True
                    token.raise_if_cancelled()
                    cached_result = result.model_copy(update={"audio_path": cache_path})
                    self._cache_results[cached_result.audio_id] = cache_path
                    await self._cleanup_cache_locked()
                except BaseException:
                    await _finish_cleanup(
                        self._cleanup_failed_promotion(
                            cached_result,
                            cache_part,
                            cache_path if replaced else None,
                        )
                    )
                    raise
            assert cached_result is not None
            await self._discard_path(source)
            return cached_result
        except asyncio.CancelledError:
            await _finish_cleanup(
                self._cleanup_cancelled_promotion(
                    cached_result,
                    source,
                    cache_part,
                    cache_path if replaced else None,
                )
            )
            raise
        except OSError:
            return result

    async def _cleanup_failed_promotion(
        self,
        cached_result: AudioResult | None,
        cache_part: Path | None,
        cache_path: Path | None,
    ) -> None:
        if cached_result is not None:
            self._cache_results.pop(cached_result.audio_id, None)
        if cache_part is not None:
            await _run_to_thread(_safe_unlink, cache_part)
        if cache_path is not None:
            await _run_to_thread(_safe_unlink, cache_path)

    async def _cleanup_cancelled_promotion(
        self,
        cached_result: AudioResult | None,
        source: Path,
        cache_part: Path | None,
        cache_path: Path | None,
    ) -> None:
        await self._cleanup_failed_promotion(cached_result, cache_part, cache_path)
        await self._discard_path(source)
        await self._cleanup_cache()

    async def _cleanup_cache(self) -> None:
        if not self._cache_enabled or self._cache_dir is None:
            return
        async with self._cache_maintenance_lock:
            await self._cleanup_cache_locked()

    async def _cleanup_cache_locked(self) -> None:
        assert self._cache_dir is not None
        leased = frozenset(self._cache_results.values())
        await _run_to_thread(
            _cleanup_cache_directory,
            self._cache_dir,
            self._cache_max_bytes,
            self._cache_ttl_seconds,
            leased,
        )

    def _result_path(self, job: TTSJob, segment_index: int) -> Path:
        turn_key = hashlib.sha256(job.turn_id.encode()).hexdigest()[:16]
        job_key = hashlib.sha256(job.job_id.encode()).hexdigest()[:20]
        return self._output_directory / turn_key / f"{segment_index:04d}-{job_key}.wav"

    @staticmethod
    def _response_error(response: httpx.Response) -> str | None:
        status = response.status_code
        if 200 <= status < 300:
            return None
        if 300 <= status < 400:
            return "tts_protocol_error"
        if status in {401, 403}:
            return "tts_auth_failed"
        if status == 404:
            return "tts_protocol_error"
        if status == 408:
            return "tts_timeout"
        if status == 429:
            return "tts_rate_limited"
        if status >= 500:
            return "tts_unavailable"
        return "tts_request_rejected"

    @staticmethod
    def _failure(job: TTSJob, error_code: str) -> AudioResult:
        return AudioResult(
            job_id=job.job_id,
            turn_id=job.turn_id,
            segment_id=job.segment_id,
            success=False,
            error_code=error_code,
        )

    async def discard(self, result: AudioResult) -> None:
        if self._cache_results.pop(result.audio_id, None) is not None:
            await self._cleanup_cache()
            return
        if result.audio_path is None or result.audio_path not in self._paths:
            return
        await self._discard_path(result.audio_path)

    async def _discard_path(self, path: Path) -> None:
        self._paths.discard(path)
        await self._delete_temp_path(path)
        await _run_to_thread(_remove_empty_parent, path.parent)

    async def close(self) -> None:
        task = self._close_task
        if task is None:
            # No await occurs before the terminal state is visible, so new
            # synthesis calls cannot slip between close and task registration.
            self._closed = True
            task = asyncio.create_task(self._close(), name="gpt-sovits-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close(self) -> None:
        while self._synthesis_tasks or self._synthesis_calls:
            workers = tuple(self._synthesis_tasks)
            calls = tuple(self._synthesis_calls)
            for worker in workers:
                self._cancel_synthesis(worker)
            await asyncio.gather(
                *(_join_task(worker, 1.0) for worker in workers),
                *(asyncio.shield(call) for call in calls),
                return_exceptions=True,
            )

        self._cache_results.clear()
        paths = tuple(self._paths)
        self._paths.clear()
        await asyncio.gather(*(self._delete_temp_path(path) for path in paths))
        parents = {path.parent for path in paths}
        await asyncio.gather(*(_run_to_thread(_remove_empty_parent, path) for path in parents))
        await self._cleanup_cache()
        await self._client.aclose()

    def _register_temp_path(self, path: Path, kind: TempAssetKind) -> None:
        if self._temp_registry is None:
            return
        entry = self._temp_registry.register(path, kind)
        self._asset_ids[path] = entry.asset_id

    def _move_temp_path(self, source: Path, target: Path, kind: TempAssetKind) -> None:
        asset_id = self._asset_ids.get(source)
        if self._temp_registry is None or asset_id is None:
            return
        self._temp_registry.mark_moved(asset_id, target, kind=kind)
        self._asset_ids.pop(source, None)
        self._asset_ids[target] = asset_id

    async def _delete_temp_path(self, path: Path) -> None:
        asset_id = self._asset_ids.pop(path, None)
        if self._temp_registry is not None and asset_id is not None:
            await _run_to_thread(
                self._temp_registry.delete,
                asset_id,
                ignore_retry_deadline=True,
            )
            return
        await _run_to_thread(path.unlink, missing_ok=True)

    def _cancel_synthesis(self, task: asyncio.Task[AudioResult]) -> None:
        if task.done() or task in self._synthesis_cancellations:
            return
        self._synthesis_cancellations.add(task)
        task.cancel()

    def _synthesis_finished(self, task: asyncio.Task[AudioResult]) -> None:
        self._synthesis_tasks.discard(task)
        self._synthesis_cancellations.discard(task)


async def _await_with_token(awaitable: Awaitable[_T], token: CancellationToken) -> _T:
    token.raise_if_cancelled()
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(token.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation, cancellation}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancellation in done:
            token.raise_if_cancelled()
        return operation.result()
    finally:
        for task in (operation, cancellation):
            if not task.done():
                task.cancel()
        await _finish_cleanup(asyncio.gather(operation, cancellation, return_exceptions=True))


async def _run_to_thread(
    function: Callable[..., _T],
    /,
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Run a file operation to completion even if its waiter is cancelled."""

    return await _finish_cleanup(asyncio.to_thread(function, *args, **kwargs))


async def _finish_cleanup(awaitable: Awaitable[_T]) -> _T:
    """Drain one owned operation before preserving any cancellation request."""

    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                raise


async def _join_task(task: asyncio.Task[Any], timeout_seconds: float) -> bool:
    """Join a task without letting repeated waiter cancellation orphan it."""

    cancelled = False
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not task.done() and asyncio.get_running_loop().time() < deadline:
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except TimeoutError:
            break
        except asyncio.CancelledError:
            if not task.done():
                cancelled = True
        except Exception:
            break
    if task.done():
        await asyncio.gather(task, return_exceptions=True)
    if cancelled:
        raise asyncio.CancelledError
    return task.done()


def _has_ssl_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _validate_reference_scope(endpoint: httpx.URL, preset: GPTSoVITSPreset) -> None:
    """Fail closed without exposing a configured path in the error text."""

    if preset.ref_audio_scope == "service_resource":
        return
    host = endpoint.host
    loopback = host == "localhost"
    if host is not None and not loopback:
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ValueError("local_file reference requires a loopback GPT-SoVITS service")
    try:
        reference = Path(preset.ref_audio_path)
        exists = reference.is_absolute() and reference.is_file()
    except OSError:
        exists = False
    if not exists:
        raise ValueError("local_file GPT-SoVITS reference is unavailable")


def _inspect_wave(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
            compression = audio.getcomptype()
            frames = audio.readframes(frame_count)
    except (EOFError, OSError, wave.Error) as exc:
        raise _AudioValidationError from exc
    if channels not in {1, 2} or sample_width not in {1, 2, 3, 4}:
        raise _AudioValidationError
    if sample_rate <= 0 or frame_count <= 0 or compression != "NONE":
        raise _AudioValidationError
    if len(frames) != frame_count * channels * sample_width:
        raise _AudioValidationError
    return sample_rate, max(1, round(frame_count / sample_rate * 1000))


def _cleanup_cache_directory(
    cache_dir: Path,
    max_bytes: int,
    ttl_seconds: float,
    leased: frozenset[Path],
) -> None:
    try:
        candidates = tuple(cache_dir.glob("*.wav"))
        partials = tuple(cache_dir.glob(".*.part"))
    except OSError:
        return
    now = time.time()
    for partial in partials:
        try:
            if now - partial.stat().st_mtime > _CACHE_PARTIAL_MAX_AGE_SECONDS:
                partial.unlink(missing_ok=True)
        except OSError:
            continue
    entries: list[tuple[Path, int, float]] = []
    for path in candidates:
        try:
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            file_stat = path.stat()
            if not path.is_file():
                continue
            if path not in leased and now - file_stat.st_mtime > ttl_seconds:
                path.unlink(missing_ok=True)
                continue
            entries.append((path, file_stat.st_size, file_stat.st_mtime))
        except OSError:
            continue

    total_bytes = sum(size for _path, size, _modified in entries)
    if total_bytes <= max_bytes:
        return
    for path, size, _modified in sorted(entries, key=lambda entry: entry[2]):
        if path in leased:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        total_bytes -= size
        if total_bytes <= max_bytes:
            return


def _remove_empty_parent(parent: Path) -> None:
    with suppress(FileNotFoundError, OSError):
        parent.rmdir()


def _safe_unlink(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)
