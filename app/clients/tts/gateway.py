"""Authenticated client for the private, path-free GPT-SoVITS gateway."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import ssl
import wave
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

import httpx

from app.clients.tts.gpt_sovits import GPTSoVITSProbe
from app.core.cancellation import CancellationToken
from app.limits import LimitsConfig
from app.schemas import AudioResult, TTSJob
from app.temp_assets import TempAssetKind, TempAssetRegistry, TempRegistryError
from app.tts_gateway.contracts import VOICE_SLOTS, GatewayHealth

_T = TypeVar("_T")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_WAVE_CONTENT_TYPES = frozenset({"audio/wav", "audio/wave", "audio/x-wav"})
_EXPECTED_BASE_URL = "http://127.0.0.1:9880"
_PROTOCOL_HEADER = "x-tts-gateway-protocol"

# The W29 structured pipeline supplies a canonical gateway slot in ``style``.
# W30's compatibility path can still receive the older bounded emotion labels,
# which are mapped only when ``style`` is not already a valid slot.
_EMOTION_TO_VOICE_SLOT: dict[str, str] = {
    "neutral": "neutral",
    "bored": "neutral",
    "sleepy": "neutral",
    "happy": "gentle",
    "worried": "gentle",
    "shy": "tsundere",
    "angry_cute": "tsundere",
    "proud": "focused",
    "focused": "focused",
    "excited": "excited_explosion",
    "explosion_mode": "excited_explosion",
}


class _AudioValidationError(ValueError):
    pass


class _AudioTooLargeError(ValueError):
    pass


class _FirstByteTimeoutError(TimeoutError):
    pass


class GPTSoVITSGatewayProvider:
    """Call only the authenticated W29 gateway and atomically own its WAV output."""

    def __init__(
        self,
        base_url: str,
        output_directory: Path,
        bearer_token: str,
        *,
        max_audio_bytes: int = 32 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        temp_registry: TempAssetRegistry | None = None,
        max_owned_synthesis_tasks: int = LimitsConfig().tts_queue_capacity,
    ) -> None:
        if base_url.rstrip("/") != _EXPECTED_BASE_URL:
            raise ValueError("private TTS gateway endpoint must be fixed loopback")
        if _TOKEN.fullmatch(bearer_token) is None:
            raise ValueError("private TTS gateway token must contain 256 bits")
        if max_audio_bytes < 44:
            raise ValueError("max_audio_bytes must fit a WAV header")
        if not 1 <= max_owned_synthesis_tasks <= LimitsConfig().tts_queue_capacity:
            raise ValueError("max owned synthesis tasks exceeds W07 TTS queue budget")
        self._output_directory = output_directory
        self._bearer_token = bearer_token
        self._max_audio_bytes = max_audio_bytes
        self._temp_registry = temp_registry
        self._tts_endpoint = httpx.URL(f"{_EXPECTED_BASE_URL}/v1/tts")
        self._health_endpoint = httpx.URL(f"{_EXPECTED_BASE_URL}/v1/health")
        self._client = httpx.AsyncClient(
            timeout=None,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._paths: set[Path] = set()
        self._asset_ids: dict[Path, str] = {}
        self._synthesis_tasks: set[asyncio.Task[AudioResult]] = set()
        self._synthesis_cancellations: set[asyncio.Task[AudioResult]] = set()
        self._synthesis_calls: set[asyncio.Future[None]] = set()
        self._synthesis_capacity = asyncio.BoundedSemaphore(max_owned_synthesis_tasks)
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    async def probe(self, *, timeout_ms: int) -> GPTSoVITSProbe:
        if timeout_ms <= 0:
            raise ValueError("gateway probe timeout must be positive")
        if self._closed:
            return GPTSoVITSProbe(False, None, error_code="tts_closed")
        response: httpx.Response | None = None
        try:
            request = self._request("GET", self._health_endpoint)
            request.extensions["timeout"] = _timeout_extensions(timeout_ms)
            response = await asyncio.wait_for(
                self._client.send(request, stream=True),
                timeout=timeout_ms / 1000,
            )
            body = await _read_bounded(response, 4 * 1024)
        except (TimeoutError, httpx.TimeoutException):
            return GPTSoVITSProbe(False, None, error_code="tts_timeout")
        except ValueError:
            return GPTSoVITSProbe(False, None, error_code="tts_protocol_error")
        except httpx.RequestError:
            return GPTSoVITSProbe(False, None, error_code="tts_unavailable")
        finally:
            if response is not None:
                await response.aclose()
        if response.status_code == 200 and _valid_gateway_response(response):
            try:
                snapshot = GatewayHealth.model_validate_json(body, strict=True)
            except ValueError:
                return GPTSoVITSProbe(False, None, error_code="tts_protocol_error")
            if snapshot.status == "ready":
                return GPTSoVITSProbe(True, "gateway_v1", status_code=200)
        return GPTSoVITSProbe(
            False,
            None,
            status_code=response.status_code,
            error_code=_status_error(response.status_code),
        )

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
        if self._synthesis_cancellations:
            return self._failure(job, "tts_cancel_timeout")
        voice_slot = _gateway_voice_slot(job)
        if voice_slot is None or not 0.5 <= job.speed_factor <= 2.0:
            return self._failure(job, "tts_voice_slot_invalid")

        call_done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._synthesis_calls.add(call_done)
        deadline = asyncio.get_running_loop().time() + job.timeout_ms / 1000
        slot_acquired = False
        worker: asyncio.Task[AudioResult] | None = None
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self._failure(job, "tts_total_timeout")
            if not await _acquire_semaphore_with_token(
                self._synthesis_capacity,
                token,
                timeout_seconds=remaining,
            ):
                return self._failure(job, "tts_total_timeout")
            slot_acquired = True
            if self._is_closed():
                return self._failure(job, "tts_closed")
            worker = asyncio.create_task(
                self._synthesize(
                    job,
                    voice_slot=voice_slot,
                    segment_index=segment_index,
                    token=token,
                ),
                name=f"gpt-sovits-gateway-{job.job_id}",
            )
            self._synthesis_tasks.add(worker)
            worker.add_done_callback(self._synthesis_finished)
            slot_acquired = False
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            return await asyncio.wait_for(asyncio.shield(worker), timeout=remaining)
        except TimeoutError:
            if worker is None:
                return self._failure(job, "tts_total_timeout")
            self._cancel_synthesis(worker)
            settled = await _join_task(worker, job.cancellation_timeout_ms / 1000)
            return self._failure(
                job,
                "tts_total_timeout" if settled else "tts_cancel_timeout",
            )
        except asyncio.CancelledError:
            if worker is not None:
                self._cancel_synthesis(worker)
                await _join_task(worker, job.cancellation_timeout_ms / 1000)
            raise
        finally:
            if slot_acquired:
                self._synthesis_capacity.release()
            self._synthesis_calls.discard(call_done)
            if not call_done.done():
                call_done.set_result(None)

    async def _synthesize(
        self,
        job: TTSJob,
        *,
        voice_slot: str,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult:
        final_path = self._result_path(job, segment_index)
        part_path = final_path.with_name(f".{final_path.name}.{uuid4().hex}.part")
        response: httpx.Response | None = None
        replace_started = False
        keep_final = False
        try:
            await _run_to_thread(
                self._register_temp_path,
                part_path,
                TempAssetKind.tts_part,
            )
            request = self._request(
                "POST",
                self._tts_endpoint,
                json={
                    "text": job.text,
                    "voice_slot": voice_slot,
                    "speed_factor": job.speed_factor,
                },
            )
            request.extensions["timeout"] = {
                "connect": job.connect_timeout_ms / 1000,
                "read": None,
                "write": None,
                "pool": job.connect_timeout_ms / 1000,
            }
            response = await _await_with_token(self._client.send(request, stream=True), token)
            if response.status_code != 200:
                return self._failure(job, _status_error(response.status_code))
            if not _valid_gateway_response(response):
                return self._failure(job, "tts_protocol_error")
            content_type = response.headers.get("content-type", "").partition(";")[0].lower()
            if content_type not in _WAVE_CONTENT_TYPES:
                return self._failure(job, "tts_invalid_content_type")
            declared = _content_length(response)
            if declared is not None and declared > self._max_audio_bytes:
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
            keep_final = True
            return AudioResult(
                job_id=job.job_id,
                turn_id=job.turn_id,
                segment_id=job.segment_id,
                success=True,
                audio_path=final_path,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
            )
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
                self._cleanup_response(
                    response,
                    part_path,
                    final_path if replace_started and not keep_final else None,
                )
            )

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

    async def _cleanup_response(
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

    async def discard(self, result: AudioResult) -> None:
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
            self._closed = True
            task = asyncio.create_task(self._close(), name="gpt-sovits-gateway-close")
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
        paths = tuple(self._paths)
        self._paths.clear()
        await asyncio.gather(*(self._delete_temp_path(path) for path in paths))
        await asyncio.gather(*(_run_to_thread(_remove_empty_parent, path.parent) for path in paths))
        await self._client.aclose()

    def _request(
        self,
        method: str,
        url: httpx.URL,
        *,
        json: dict[str, object] | None = None,
    ) -> httpx.Request:
        return self._client.build_request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "X-TTS-Gateway-Protocol": "1",
            },
            json=json,
        )

    def _result_path(self, job: TTSJob, segment_index: int) -> Path:
        turn_key = hashlib.sha256(job.turn_id.encode()).hexdigest()[:16]
        job_key = hashlib.sha256(job.job_id.encode()).hexdigest()[:20]
        return self._output_directory / turn_key / f"{segment_index:04d}-{job_key}.wav"

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
        # A worker owns exactly one permit after ``synthesize`` hands off the
        # acquired slot.  Keep the membership guard so an accidental duplicate
        # callback cannot over-release BoundedSemaphore, including if a task is
        # cancelled before its coroutine receives its first scheduling slice.
        if task not in self._synthesis_tasks:
            return
        self._synthesis_tasks.remove(task)
        self._synthesis_cancellations.discard(task)
        self._synthesis_capacity.release()

    def _is_closed(self) -> bool:
        """Re-read close state after an awaited capacity handoff."""

        return self._closed

    @staticmethod
    def _failure(job: TTSJob, error_code: str) -> AudioResult:
        return AudioResult(
            job_id=job.job_id,
            turn_id=job.turn_id,
            segment_id=job.segment_id,
            success=False,
            error_code=error_code,
        )


def _gateway_voice_slot(job: TTSJob) -> str | None:
    """Prefer W29's canonical style and retain W30 compatibility labels."""

    if job.style in VOICE_SLOTS:
        return job.style
    if job.emotion in VOICE_SLOTS:
        return job.emotion
    return _EMOTION_TO_VOICE_SLOT.get(job.emotion)


def _timeout_extensions(timeout_ms: int) -> dict[str, float]:
    seconds = timeout_ms / 1000
    return {
        "connect": seconds,
        "read": seconds,
        "write": seconds,
        "pool": seconds,
    }


def _valid_gateway_response(response: httpx.Response) -> bool:
    return str(response.headers.get(_PROTOCOL_HEADER, "")) == "1"


def _status_error(status: int) -> str:
    if status in {401, 403}:
        return "tts_auth_failed"
    if status == 429:
        return "tts_rate_limited"
    if status in {408, 504}:
        return "tts_timeout"
    if status >= 500:
        return "tts_unavailable"
    return "tts_protocol_error"


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    output = bytearray()
    async for chunk in response.aiter_bytes():
        output.extend(chunk)
        if len(output) > limit:
            raise ValueError("bounded gateway response exceeded")
    return bytes(output)


async def _await_with_token(awaitable: Awaitable[_T], token: CancellationToken) -> _T:
    token.raise_if_cancelled()
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(token.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation, cancellation},
            return_when=asyncio.FIRST_COMPLETED,
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
    return await _finish_cleanup(asyncio.to_thread(function, *args, **kwargs))


async def _finish_cleanup(awaitable: Awaitable[_T]) -> _T:
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


async def _acquire_semaphore_with_token(
    semaphore: asyncio.Semaphore,
    token: CancellationToken,
    *,
    timeout_seconds: float,
) -> bool:
    token.raise_if_cancelled()
    acquisition = asyncio.create_task(semaphore.acquire())
    cancellation = asyncio.create_task(token.wait())
    acquired = False
    try:
        done, _pending = await asyncio.wait(
            {acquisition, cancellation},
            timeout=max(0.0, timeout_seconds),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation in done:
            token.raise_if_cancelled()
        if acquisition in done:
            acquisition.result()
            acquired = True
            return True
        return False
    finally:
        cancellation.cancel()
        if not acquired:
            if not acquisition.done():
                acquisition.cancel()
            await asyncio.gather(acquisition, return_exceptions=True)
            if (
                acquisition.done()
                and not acquisition.cancelled()
                and acquisition.exception() is None
            ):
                semaphore.release()
        await asyncio.gather(cancellation, return_exceptions=True)


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


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _has_ssl_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _remove_empty_parent(parent: Path) -> None:
    with suppress(FileNotFoundError, OSError):
        parent.rmdir()
