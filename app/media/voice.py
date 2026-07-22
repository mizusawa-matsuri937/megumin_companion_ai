"""Parent-side PTT controller backed by a supervised private MediaWorker.

The desktop/backend process owns only state, bounded metadata, and the final
transcript.  It never imports a microphone binding, receives PCM/WAV/JSON, or
starts ``whisper-cli`` itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.config import ConfigurationError, Settings
from app.temp_assets import TempAssetKind, TempAssetRegistry
from app.windows_security import directory_security_for_current_platform
from app.workers import (
    ApprovedResourcePolicy,
    ResourceReference,
    SupervisorConfig,
    WorkerSupervisor,
    process_adapter_for_current_platform,
)

_ROOT_STT_TEMP = "stt_temp"
_VOICE_DIRECTORY_PREFIX = "companion-recording-"
_START_DEADLINE_SECONDS = 15.0
_CANCEL_DEADLINE_SECONDS = 10.0


class VoiceCaptureState(StrEnum):
    idle = "idle"
    recording = "recording"
    timed_out = "timed_out"
    transcribing = "transcribing"
    closed = "closed"


class VoiceCaptureError(RuntimeError):
    """Stable, content-free failure from the local microphone/STT boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VoiceTranscription:
    text: str
    language: str | None
    segment_count: int

    def __post_init__(self) -> None:
        normalized = self.text.strip()
        if not normalized or "\x00" in normalized or len(normalized) > 4_096:
            raise ValueError("voice transcription is outside the helper boundary")
        if self.language is not None and (
            not self.language or "\x00" in self.language or len(self.language) > 64
        ):
            raise ValueError("voice transcription language is invalid")
        if self.segment_count < 0:
            raise ValueError("voice transcription segment count is invalid")
        object.__setattr__(self, "text", normalized)


class _MediaSupervisor(Protocol):
    async def start(self) -> None: ...

    async def run_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        resources: Sequence[ResourceReference] = (),
        hard_deadline_seconds: float | None = None,
    ) -> dict[str, Any]: ...

    async def stop(self) -> object: ...


@dataclass(slots=True)
class _ActiveRecording:
    job_id: str
    directory: Path
    asset_id: str | None
    watchdog: asyncio.Task[None]


class MediaWorkerVoiceInput:
    """One explicit PTT capture at a time, with no persistent microphone open."""

    def __init__(
        self,
        *,
        roots: Mapping[str, Path],
        maximum_recording_seconds: float,
        transcription_timeout_seconds: float,
        supervisor: _MediaSupervisor | None = None,
        supervisor_factory: Callable[[], _MediaSupervisor] | None = None,
        prepare_roots: Callable[[], None] | None = None,
        temp_registry: TempAssetRegistry | None = None,
        watchdog_wait: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if (supervisor is None) == (supervisor_factory is None):
            raise ValueError("voice input requires exactly one supervisor source")
        selected_roots = {key: value.absolute() for key, value in roots.items()}
        if set(selected_roots) != {_ROOT_STT_TEMP}:
            raise ValueError("voice input requires exactly one private STT root")
        if not _valid_duration(maximum_recording_seconds, maximum=120.0):
            raise ValueError("voice recording maximum must be within 120 seconds")
        if not _valid_duration(transcription_timeout_seconds, maximum=120.0):
            raise ValueError("voice transcription timeout must be within 120 seconds")
        self._roots = selected_roots
        self._maximum_recording_seconds = float(maximum_recording_seconds)
        self._transcription_timeout_seconds = float(transcription_timeout_seconds)
        self._supervisor = supervisor
        self._supervisor_factory = supervisor_factory
        self._prepare_roots = prepare_roots
        self._temp_registry = temp_registry
        self._watchdog_wait = watchdog_wait or asyncio.sleep
        self._state = VoiceCaptureState.idle
        self._active: _ActiveRecording | None = None
        self._transcription_task: asyncio.Task[VoiceTranscription] | None = None
        self._lock = asyncio.Lock()
        self._cancel_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def state(self) -> VoiceCaptureState:
        return self._state

    @classmethod
    def for_settings(
        cls,
        settings: Settings,
        *,
        temp_registry: TempAssetRegistry | None = None,
    ) -> MediaWorkerVoiceInput:
        try:
            root = settings.stt_temporary_directory().absolute()
            executable = settings.stt_executable_path().absolute()
            model = settings.stt_model_path().absolute()
        except (ConfigurationError, ValueError) as exc:
            raise VoiceCaptureError("stt_config_invalid") from exc
        roots = {_ROOT_STT_TEMP: root}
        client: MediaWorkerVoiceInput

        def prepare_roots() -> None:
            directory_security_for_current_platform().ensure_private_tree(
                settings.paths.root,
                (root,),
            )

        def make_supervisor() -> WorkerSupervisor:
            prepare_roots()
            return WorkerSupervisor(
                name="media_stt",
                role="media",
                command=_voice_worker_command(
                    roots,
                    executable=executable,
                    model=model,
                    language=settings.stt.language,
                    threads=settings.stt.threads,
                    terminate_grace_seconds=settings.stt.terminate_grace_seconds,
                    max_audio_bytes=settings.stt.max_audio_bytes,
                    max_output_bytes=settings.stt.max_output_bytes,
                    maximum_recording_seconds=settings.stt.max_recording_seconds,
                    transcription_timeout_seconds=settings.stt.transcription_timeout_seconds,
                    input_device=settings.stt.device,
                    input_blocksize=settings.stt.blocksize,
                ),
                adapter=process_adapter_for_current_platform(),
                resource_policy=ApprovedResourcePolicy(roots=roots),
                temp_scavenger=client._scavenge_active_recording,
                config=SupervisorConfig(
                    maximum_active_jobs=1,
                    maximum_job_seconds=max(
                        _START_DEADLINE_SECONDS + 2.0,
                        settings.stt.transcription_timeout_seconds + 5.0,
                    ),
                ),
            )

        client = cls(
            roots=roots,
            maximum_recording_seconds=settings.stt.max_recording_seconds,
            transcription_timeout_seconds=settings.stt.transcription_timeout_seconds,
            supervisor_factory=make_supervisor,
            prepare_roots=prepare_roots,
            temp_registry=temp_registry,
        )
        return client

    async def start(self) -> None:
        async with self._lock:
            if self._closed or self._state is not VoiceCaptureState.idle:
                raise VoiceCaptureError("voice_invalid_state")
            job_id = f"sttstart{uuid4().hex}"
            directory = _recording_directory(self._roots[_ROOT_STT_TEMP], job_id)
            asset_id = await self._register_directory(directory)
            self._state = VoiceCaptureState.recording
        try:
            supervisor = await self._ensure_supervisor()
            payload = await supervisor.run_job(
                job_id=job_id,
                job_kind="media.record_start",
                hard_deadline_seconds=_START_DEADLINE_SECONDS,
            )
            _require_recording_started(payload)
        except asyncio.CancelledError:
            # A caller can cancel after the helper has opened the microphone but
            # before this coroutine records ``_active``.  Always send the
            # idempotent worker-side cancel before releasing the registry lease.
            await self._cancel_worker_recording()
            await self._cleanup_directory(directory, asset_id)
            async with self._lock:
                if not self._closed:
                    self._state = VoiceCaptureState.idle
            raise
        except Exception as exc:
            await self._cancel_worker_recording()
            await self._cleanup_directory(directory, asset_id)
            async with self._lock:
                if not self._closed:
                    self._state = VoiceCaptureState.idle
            raise _voice_error_from_exception(exc) from exc
        watchdog = asyncio.create_task(
            self._watch_recording_limit(job_id),
            name=f"voice-recording-limit-{job_id}",
        )
        invalid_state = False
        async with self._lock:
            if self._closed or self._state is not VoiceCaptureState.recording:
                invalid_state = True
            else:
                self._active = _ActiveRecording(job_id, directory, asset_id, watchdog)
        if invalid_state:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            await self._cancel_worker_recording()
            await self._cleanup_directory(directory, asset_id)
            raise VoiceCaptureError("voice_invalid_state")

    async def preflight(self) -> None:
        """Validate the configured local runtime without opening a microphone."""

        async with self._lock:
            if self._closed:
                raise VoiceCaptureError("voice_closed")
            if self._state is not VoiceCaptureState.idle:
                raise VoiceCaptureError("voice_invalid_state")
        try:
            supervisor = await self._ensure_supervisor()
            payload = await supervisor.run_job(
                job_id=f"sttpreflight{uuid4().hex}",
                job_kind="media.stt_preflight",
                hard_deadline_seconds=_START_DEADLINE_SECONDS,
            )
        except Exception as exc:
            raise _voice_error_from_exception(exc) from exc
        _require_stt_preflight(payload)

    async def stop(self) -> VoiceTranscription:
        owner = asyncio.current_task()
        assert owner is not None
        async with self._lock:
            active = self._active
            if active is None or self._state not in {
                VoiceCaptureState.recording,
                VoiceCaptureState.timed_out,
            }:
                raise VoiceCaptureError("voice_invalid_state")
            timed_out = self._state is VoiceCaptureState.timed_out
            self._state = VoiceCaptureState.transcribing
            self._transcription_task = owner
        active.watchdog.cancel()
        await asyncio.gather(active.watchdog, return_exceptions=True)
        try:
            if timed_out:
                await self._cancel_worker_recording()
                raise VoiceCaptureError("stt_recording_too_long")
            supervisor = await self._ensure_supervisor()
            payload = await supervisor.run_job(
                job_id=f"sttstop{uuid4().hex}",
                job_kind="media.record_stop",
                hard_deadline_seconds=self._transcription_timeout_seconds + 4.0,
            )
            return _parse_transcription(payload)
        except asyncio.CancelledError:
            # A cancellation can win before the stop request reaches the
            # helper.  Send the idempotent capture cancel as a fail-closed
            # fallback so an already-open input stream cannot outlive this
            # parent coroutine.
            await self._cancel_worker_recording()
            raise
        except VoiceCaptureError:
            raise
        except Exception as exc:
            raise _voice_error_from_exception(exc) from exc
        finally:
            await self._finish_active(active, owner)

    async def cancel(self) -> None:
        async with self._lock:
            task = self._cancel_task
            if task is None or task.done():
                task = asyncio.create_task(self._cancel_impl(), name="media-voice-cancel")
                self._cancel_task = task
        await asyncio.shield(task)

    async def close(self) -> None:
        async with self._lock:
            task = self._close_task
            if task is None:
                self._closed = True
                task = asyncio.create_task(self._close_impl(), name="media-voice-close")
                self._close_task = task
        await asyncio.shield(task)

    async def _cancel_impl(self) -> None:
        async with self._lock:
            active = self._active
            task = self._transcription_task
            state = self._state
            if state in {VoiceCaptureState.recording, VoiceCaptureState.timed_out}:
                self._active = None
                if not self._closed:
                    self._state = VoiceCaptureState.idle
            elif state is VoiceCaptureState.transcribing and task is not None:
                task.cancel()
            else:
                return
        if active is not None and state in {
            VoiceCaptureState.recording,
            VoiceCaptureState.timed_out,
        }:
            active.watchdog.cancel()
            await asyncio.gather(active.watchdog, return_exceptions=True)
            await self._cancel_worker_recording()
            await self._cleanup_directory(active.directory, active.asset_id)
        elif task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def _close_impl(self) -> None:
        await self.cancel()
        supervisor = self._supervisor
        if supervisor is not None:
            with suppress(Exception):
                await supervisor.stop()
        async with self._lock:
            self._state = VoiceCaptureState.closed

    async def _watch_recording_limit(self, job_id: str) -> None:
        try:
            await self._watchdog_wait(self._maximum_recording_seconds)
        except asyncio.CancelledError:
            raise
        async with self._lock:
            active = self._active
            if (
                active is None
                or active.job_id != job_id
                or self._state is not VoiceCaptureState.recording
            ):
                return
            self._state = VoiceCaptureState.timed_out
        await self._cancel_worker_recording()
        # The user may never release a button after a lost focus/lock event.
        # Delete the private recording promptly; a later ``stop`` reports the
        # stable timeout code and treats the already-deleted lease as settled.
        await self._cleanup_directory(active.directory, active.asset_id)

    async def _finish_active(
        self,
        active: _ActiveRecording,
        owner: asyncio.Task[object],
    ) -> None:
        await self._cleanup_directory(active.directory, active.asset_id)
        async with self._lock:
            if self._active is active:
                self._active = None
            if self._transcription_task is owner:
                self._transcription_task = None
            if not self._closed:
                self._state = VoiceCaptureState.idle

    async def _ensure_supervisor(self) -> _MediaSupervisor:
        async with self._lock:
            if self._closed:
                raise VoiceCaptureError("voice_closed")
            supervisor = self._supervisor
            if supervisor is None:
                if self._prepare_roots is not None:
                    self._prepare_roots()
                assert self._supervisor_factory is not None
                supervisor = self._supervisor_factory()
                self._supervisor = supervisor
        await supervisor.start()
        return supervisor

    async def _cancel_worker_recording(self) -> None:
        async with self._lock:
            supervisor = self._supervisor
        if supervisor is None:
            return
        try:
            await supervisor.run_job(
                job_id=f"sttcancel{uuid4().hex}",
                job_kind="media.record_cancel",
                hard_deadline_seconds=_CANCEL_DEADLINE_SECONDS,
            )
        except Exception:
            return

    async def _register_directory(self, directory: Path) -> str | None:
        registry = self._temp_registry
        if registry is None:
            return None
        entry = await asyncio.to_thread(
            registry.register,
            directory,
            TempAssetKind.recording_directory,
        )
        return entry.asset_id

    async def _cleanup_directory(self, directory: Path, asset_id: str | None) -> None:
        registry = self._temp_registry
        if registry is not None and asset_id is not None:
            result = await asyncio.to_thread(
                registry.delete,
                asset_id,
                ignore_retry_deadline=True,
            )
            if result.status.value not in {"deleted", "missing"}:
                raise VoiceCaptureError("stt_temp_cleanup_pending")
            return
        try:
            await asyncio.to_thread(_remove_directory, self._roots[_ROOT_STT_TEMP], directory)
        except Exception as exc:
            raise VoiceCaptureError("stt_temp_cleanup_pending") from exc

    async def _scavenge_active_recording(self) -> None:
        async with self._lock:
            active = self._active
        if active is not None:
            with suppress(Exception):
                await self._cleanup_directory(active.directory, active.asset_id)


def create_media_worker_voice_input(
    settings: Settings,
    *,
    temp_registry: TempAssetRegistry | None = None,
) -> MediaWorkerVoiceInput | None:
    """Build the opt-in local PTT owner without opening a device at import time."""

    if not settings.stt.enabled:
        return None
    if settings.stt.provider.strip().lower().replace("-", "_") != "whisper_cpp":
        raise VoiceCaptureError("stt_provider_unsupported")
    return MediaWorkerVoiceInput.for_settings(settings, temp_registry=temp_registry)


def _voice_worker_command(
    roots: Mapping[str, Path],
    *,
    executable: Path,
    model: Path,
    language: str,
    threads: int | None,
    terminate_grace_seconds: float,
    max_audio_bytes: int,
    max_output_bytes: int,
    maximum_recording_seconds: float,
    transcription_timeout_seconds: float,
    input_device: int | str | None,
    input_blocksize: int,
) -> tuple[str, ...]:
    command: list[str] = [str(Path(sys.executable).resolve()), "-m", "app.media_entrypoint"]
    for root_id, root in sorted(roots.items()):
        command.extend(("--root", f"{root_id}={root.absolute()}"))
    command.extend(
        (
            "--stt-executable",
            str(executable),
            "--stt-model",
            str(model),
            "--stt-temporary-root",
            str(roots[_ROOT_STT_TEMP].absolute()),
            "--stt-language",
            language,
            "--stt-terminate-grace-seconds",
            str(terminate_grace_seconds),
            "--stt-max-audio-bytes",
            str(max_audio_bytes),
            "--stt-max-output-bytes",
            str(max_output_bytes),
            "--stt-maximum-recording-seconds",
            str(maximum_recording_seconds),
            "--stt-transcription-timeout-seconds",
            str(transcription_timeout_seconds),
        )
    )
    if threads is not None:
        command.extend(("--stt-threads", str(threads)))
    if isinstance(input_device, int) and not isinstance(input_device, bool):
        command.extend(("--input-device-index", str(input_device)))
    elif isinstance(input_device, str) and input_device:
        command.extend(("--input-device-name", input_device))
    return tuple(command)


def _recording_directory(root: Path, job_id: str) -> Path:
    digest = hashlib.sha256(job_id.encode("ascii")).hexdigest()[:32]
    return root / f"{_VOICE_DIRECTORY_PREFIX}{digest}"


def _require_recording_started(payload: object) -> None:
    if payload == {"status": "recording"}:
        return
    if isinstance(payload, dict) and payload.get("status") == "failed":
        code = payload.get("error_code")
        if isinstance(code, str) and 1 <= len(code) <= 64:
            raise VoiceCaptureError(code)
    raise VoiceCaptureError("stt_worker_protocol")


def _require_stt_preflight(payload: object) -> None:
    if payload == {"status": "ready"}:
        return
    if isinstance(payload, dict) and payload.get("status") == "failed":
        code = payload.get("error_code")
        if isinstance(code, str) and 1 <= len(code) <= 64:
            raise VoiceCaptureError(code)
    raise VoiceCaptureError("stt_worker_protocol")


def _parse_transcription(payload: object) -> VoiceTranscription:
    if isinstance(payload, dict) and payload.get("status") == "failed":
        code = payload.get("error_code")
        if isinstance(code, str) and 1 <= len(code) <= 64:
            raise VoiceCaptureError(code)
    if payload == {"status": "cancelled"}:
        raise VoiceCaptureError("voice_cancelled")
    if not isinstance(payload, dict) or set(payload) != {
        "status",
        "text",
        "language",
        "segment_count",
    }:
        raise VoiceCaptureError("stt_worker_protocol")
    if payload["status"] != "transcribed":
        raise VoiceCaptureError("stt_worker_protocol")
    text = payload["text"]
    language = payload["language"]
    segments = payload["segment_count"]
    if (
        not isinstance(text, str)
        or not isinstance(language, str)
        or (isinstance(segments, bool) or not isinstance(segments, int))
    ):
        raise VoiceCaptureError("stt_worker_protocol")
    try:
        return VoiceTranscription(text, language or None, segments)
    except ValueError as exc:
        raise VoiceCaptureError("stt_worker_protocol") from exc


def _voice_error_from_exception(error: BaseException) -> VoiceCaptureError:
    code = getattr(error, "code", "")
    if isinstance(code, str) and code:
        if code == "worker_platform_unsupported":
            return VoiceCaptureError("stt_worker_unavailable")
        return VoiceCaptureError(code)
    return VoiceCaptureError("stt_worker_failed")


def _valid_duration(value: object, *, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 < value <= maximum
    )


def _remove_directory(root: Path, directory: Path) -> None:
    try:
        directory.relative_to(root)
        if directory.exists():
            shutil.rmtree(directory)
    except (OSError, ValueError) as exc:
        raise VoiceCaptureError("stt_temp_cleanup_pending") from exc
