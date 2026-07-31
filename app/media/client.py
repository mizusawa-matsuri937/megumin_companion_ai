"""Parent-side AudioPlayer implementation backed by one supervised MediaWorker."""

from __future__ import annotations

import asyncio
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.config import Settings
from app.core.cancellation import CancellationToken
from app.media.types import (
    MAX_OUTPUT_DEVICES,
    AudioOutputDevice,
    MouthEnvelopeSample,
    OutputDeviceList,
)
from app.pipelines.audio_player import AudioPlaybackResult
from app.schemas import AudioResult
from app.windows_security import directory_security_for_current_platform
from app.workers import (
    ApprovedResourcePolicy,
    ResourceReference,
    SupervisorConfig,
    WorkerError,
    WorkerJobProgress,
    WorkerSupervisor,
    process_adapter_for_current_platform,
)

_ROOT_TEMP = "audio_temp"
_ROOT_CACHE = "audio_cache"
_RESOURCE_WAVE = "wave"
_PLAYBACK_DEADLINE_SECONDS = 125.0
_REVIEW_PLAYBACK_DEADLINE_SECONDS = 25.0
_REVIEW_MAXIMUM_JOB_SECONDS = 30.0
MouthEnvelopeListener = Callable[[MouthEnvelopeSample], object]


class _MediaSupervisor(Protocol):
    async def start(self) -> None: ...

    async def run_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        resources: Sequence[ResourceReference] = (),
        hard_deadline_seconds: float | None = None,
        progress_callback: Callable[[WorkerJobProgress], None] | None = None,
    ) -> dict[str, Any]: ...

    async def cancel_job(self, job_id: str, *, deadline_at: float | None = None) -> None: ...

    async def stop(self) -> object: ...


class MediaWorkerAudioPlayer:
    """Play approved WAV assets without importing or invoking PortAudio in-process."""

    def __init__(
        self,
        *,
        roots: Mapping[str, Path],
        selected_device_id: str | None,
        supervisor: _MediaSupervisor | None = None,
        supervisor_factory: Callable[[], _MediaSupervisor] | None = None,
        prepare_roots: Callable[[], None] | None = None,
        playback_deadline_seconds: float = _PLAYBACK_DEADLINE_SECONDS,
        mouth_envelope_listener: MouthEnvelopeListener | None = None,
    ) -> None:
        if (supervisor is None) == (supervisor_factory is None):
            raise ValueError("media worker requires exactly one supervisor source")
        selected_roots = {key: value.absolute() for key, value in roots.items()}
        if not selected_roots:
            raise ValueError("media worker requires approved audio roots")
        if (
            isinstance(playback_deadline_seconds, bool)
            or not isinstance(playback_deadline_seconds, (int, float))
            or not math.isfinite(playback_deadline_seconds)
            or playback_deadline_seconds <= 0
        ):
            raise ValueError("media playback deadline must be positive and finite")
        self._roots = selected_roots
        self._selected_device_id = selected_device_id or None
        self._supervisor = supervisor
        self._supervisor_factory = supervisor_factory
        self._prepare_roots = prepare_roots
        self._playback_deadline_seconds = float(playback_deadline_seconds)
        self._mouth_envelope_listener = mouth_envelope_listener
        self._operation_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._active_job_id: str | None = None
        self._active_task: asyncio.Task[dict[str, Any]] | None = None
        self._closed = False

    @classmethod
    def for_settings(
        cls,
        settings: Settings,
        *,
        mouth_envelope_listener: MouthEnvelopeListener | None = None,
    ) -> MediaWorkerAudioPlayer:
        roots = {
            _ROOT_TEMP: settings.paths.temp / "audio",
            _ROOT_CACHE: settings.paths.audio_cache,
        }

        def prepare_roots() -> None:
            directory_security_for_current_platform().ensure_private_tree(
                settings.paths.root,
                tuple(roots.values()),
            )

        def make_supervisor() -> WorkerSupervisor:
            prepare_roots()
            policy = ApprovedResourcePolicy(roots=roots)
            command = _worker_command(
                roots,
                settings.pipeline.output_device_id,
                playback_chunk_ms=settings.avatar.playback_chunk_ms,
                mouth_noise_floor=settings.avatar.mouth_noise_floor,
                mouth_gain=settings.avatar.mouth_gain,
                mouth_attack_seconds=settings.avatar.mouth_attack_seconds,
                mouth_release_seconds=settings.avatar.mouth_release_seconds,
            )
            return WorkerSupervisor(
                name="media",
                role="media",
                command=command,
                adapter=process_adapter_for_current_platform(),
                resource_policy=policy,
                config=SupervisorConfig(
                    maximum_active_jobs=1,
                    maximum_job_seconds=130.0,
                ),
            )

        return cls(
            roots=roots,
            selected_device_id=settings.pipeline.output_device_id,
            supervisor_factory=make_supervisor,
            prepare_roots=prepare_roots,
            mouth_envelope_listener=mouth_envelope_listener,
        )

    @classmethod
    def for_review(cls, audio_root: Path) -> MediaWorkerAudioPlayer:
        """Create an isolated player for Gate A's synthetic temporary WAVs."""

        roots = {_ROOT_TEMP: audio_root.absolute()}

        def prepare_roots() -> None:
            for root in roots.values():
                root.mkdir(parents=True, exist_ok=True)

        def make_supervisor() -> WorkerSupervisor:
            prepare_roots()
            policy = ApprovedResourcePolicy(roots=roots)
            return WorkerSupervisor(
                name="media_review",
                role="media",
                command=_worker_command(roots, None),
                adapter=process_adapter_for_current_platform(),
                resource_policy=policy,
                config=SupervisorConfig(
                    maximum_active_jobs=1,
                    maximum_job_seconds=_REVIEW_MAXIMUM_JOB_SECONDS,
                ),
            )

        return cls(
            roots=roots,
            selected_device_id=None,
            supervisor_factory=make_supervisor,
            prepare_roots=prepare_roots,
            # Gate A only produces short synthetic tones. Keep its deadline below
            # the review supervisor's 30-second hard maximum rather than using
            # the production player's 125-second allowance.
            playback_deadline_seconds=_REVIEW_PLAYBACK_DEADLINE_SECONDS,
        )

    async def list_output_devices(self) -> OutputDeviceList:
        async with self._operation_lock:
            try:
                supervisor = await self._ensure_supervisor()
                payload = await supervisor.run_job(
                    job_id=_job_id("devices"),
                    job_kind="media.devices",
                    hard_deadline_seconds=10.0,
                )
            except Exception:
                return OutputDeviceList((), False, "audio_device_enumeration_failed")
            return _parse_device_list(payload)

    async def play(self, result: AudioResult, token: CancellationToken) -> AudioPlaybackResult:
        if not result.success or result.audio_path is None:
            return AudioPlaybackResult(played=False, error_code="audio_unavailable")
        try:
            reference = self._resource_reference(result.audio_path)
        except ValueError:
            return AudioPlaybackResult(played=False, error_code="audio_path_unapproved")
        async with self._operation_lock:
            try:
                supervisor = await self._ensure_supervisor()
            except Exception as exc:
                return _worker_failure(exc)
            job_id = _job_id("play")
            last_progress_sequence = 0

            def progress_callback(progress: WorkerJobProgress) -> None:
                nonlocal last_progress_sequence
                if (
                    progress.job_id != job_id
                    or progress.kind != "mouth_envelope"
                    or progress.sequence <= last_progress_sequence
                ):
                    return
                try:
                    sample = MouthEnvelopeSample(
                        turn_id=result.turn_id,
                        playback_job_id=job_id,
                        sequence=progress.sequence,
                        value=progress.value,
                    )
                except (TypeError, ValueError):
                    return
                last_progress_sequence = progress.sequence
                self._notify_mouth_envelope(sample)

            if self._mouth_envelope_listener is None:
                run_job = supervisor.run_job(
                    job_id=job_id,
                    job_kind="media.play",
                    resources=(reference,),
                    hard_deadline_seconds=self._playback_deadline_seconds,
                )
            else:
                run_job = supervisor.run_job(
                    job_id=job_id,
                    job_kind="media.play",
                    resources=(reference,),
                    hard_deadline_seconds=self._playback_deadline_seconds,
                    progress_callback=progress_callback,
                )
            task = asyncio.create_task(
                run_job,
                name=f"media-play-{job_id}",
            )
            async with self._state_lock:
                self._active_job_id = job_id
                self._active_task = task
            cancellation = asyncio.create_task(token.wait(), name=f"media-cancel-{job_id}")
            try:
                done, _pending = await asyncio.wait(
                    {task, cancellation},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation in done:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    token.raise_if_cancelled()
                payload = await task
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as exc:
                return _worker_failure(exc)
            finally:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
                async with self._state_lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None
                        self._active_task = None
                if self._mouth_envelope_listener is not None:
                    self._notify_mouth_envelope(
                        MouthEnvelopeSample(
                            turn_id=result.turn_id,
                            playback_job_id=job_id,
                            sequence=last_progress_sequence + 1,
                            value=0.0,
                            terminal=True,
                        )
                    )
            return _parse_playback_result(payload)

    async def stop(self, *, immediate: bool = False) -> None:
        async with self._state_lock:
            job_id = self._active_job_id
            active_task = self._active_task
            supervisor = self._supervisor
        if supervisor is None:
            return
        if job_id is not None:
            with suppress(Exception):
                await supervisor.cancel_job(job_id)
            current = asyncio.current_task()
            if immediate and active_task is not None and active_task is not current:
                active_task.cancel()
            return
        async with self._operation_lock:
            try:
                await supervisor.run_job(
                    job_id=_job_id("release"),
                    job_kind="media.release",
                    hard_deadline_seconds=10.0,
                )
            except Exception:
                return

    async def close(self) -> None:
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            supervisor = self._supervisor
        await self.stop(immediate=True)
        if supervisor is not None:
            try:
                await supervisor.stop()
            except Exception:
                return

    async def _ensure_supervisor(self) -> _MediaSupervisor:
        async with self._state_lock:
            if self._closed:
                raise WorkerError("audio_player_closed")
            supervisor = self._supervisor
            if supervisor is None:
                if self._prepare_roots is not None:
                    self._prepare_roots()
                assert self._supervisor_factory is not None
                supervisor = self._supervisor_factory()
                self._supervisor = supervisor
        await supervisor.start()
        return supervisor

    def _resource_reference(self, path: Path) -> ResourceReference:
        candidate = path.absolute()
        for root_id, root in self._roots.items():
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if relative.parts and ".." not in relative.parts:
                return ResourceReference(
                    resource_id=_RESOURCE_WAVE,
                    root_id=root_id,
                    relative_path=relative.as_posix(),
                )
        raise ValueError("audio path is outside approved roots")

    def _notify_mouth_envelope(self, sample: MouthEnvelopeSample) -> None:
        listener = self._mouth_envelope_listener
        if listener is None:
            return
        with suppress(Exception):
            listener(sample)


def create_media_worker_audio_player(
    settings: Settings,
    *,
    mouth_envelope_listener: MouthEnvelopeListener | None = None,
) -> MediaWorkerAudioPlayer:
    """Construct the production player lazily; no worker starts during import."""

    return MediaWorkerAudioPlayer.for_settings(
        settings,
        mouth_envelope_listener=mouth_envelope_listener,
    )


def _worker_command(
    roots: Mapping[str, Path],
    selected_device_id: str | None,
    *,
    playback_chunk_ms: float = 30.0,
    mouth_noise_floor: float = 0.02,
    mouth_gain: float = 4.0,
    mouth_attack_seconds: float = 0.04,
    mouth_release_seconds: float = 0.12,
) -> tuple[str, ...]:
    executable = str(Path(sys.executable).resolve())
    command: list[str] = [executable, "-m", "app.media_entrypoint"]
    for root_id, root in sorted(roots.items()):
        command.extend(("--root", f"{root_id}={root.absolute()}"))
    command.extend(
        (
            "--playback-chunk-ms",
            str(playback_chunk_ms),
            "--mouth-noise-floor",
            str(mouth_noise_floor),
            "--mouth-gain",
            str(mouth_gain),
            "--mouth-attack-seconds",
            str(mouth_attack_seconds),
            "--mouth-release-seconds",
            str(mouth_release_seconds),
        )
    )
    if selected_device_id:
        command.extend(("--output-device-id", selected_device_id))
    return tuple(command)


def _job_id(kind: str) -> str:
    return f"media_{kind}_{uuid4().hex}"


def _worker_failure(error: BaseException) -> AudioPlaybackResult:
    code = getattr(error, "code", "")
    if code in {"worker_platform_unsupported", "audio_player_closed"}:
        return AudioPlaybackResult(played=False, error_code="audio_worker_unavailable")
    if code in {"worker_job_deadline", "worker_heartbeat_lost"}:
        return AudioPlaybackResult(played=False, error_code="audio_worker_hung")
    return AudioPlaybackResult(played=False, error_code="audio_worker_failed")


def _parse_device_list(payload: object) -> OutputDeviceList:
    if not isinstance(payload, dict) or set(payload) != {"status", "devices", "truncated"}:
        return OutputDeviceList((), False, "audio_device_enumeration_failed")
    if payload.get("status") != "ok" or not isinstance(payload["devices"], list):
        return OutputDeviceList((), False, "audio_device_enumeration_failed")
    devices: list[AudioOutputDevice] = []
    for raw in payload["devices"]:
        if not isinstance(raw, dict) or set(raw) != {"device_id", "label", "is_default"}:
            return OutputDeviceList((), False, "audio_device_enumeration_failed")
        try:
            device = AudioOutputDevice(
                device_id=raw["device_id"],
                label=raw["label"],
                is_default=raw["is_default"],
            )
        except (TypeError, ValueError):
            return OutputDeviceList((), False, "audio_device_enumeration_failed")
        if not isinstance(raw["is_default"], bool):
            return OutputDeviceList((), False, "audio_device_enumeration_failed")
        devices.append(device)
    if len(devices) > MAX_OUTPUT_DEVICES or not isinstance(payload["truncated"], bool):
        return OutputDeviceList((), False, "audio_device_enumeration_failed")
    return OutputDeviceList(tuple(devices), payload["truncated"])


def _parse_playback_result(payload: object) -> AudioPlaybackResult:
    if not isinstance(payload, dict):
        return AudioPlaybackResult(played=False, error_code="audio_worker_protocol")
    status = payload.get("status")
    if status == "played" and set(payload) == {"status", "notice_code"}:
        notice_code = payload["notice_code"]
        if isinstance(notice_code, str) and len(notice_code) <= 64:
            return AudioPlaybackResult(played=True, notice_code=notice_code or None)
    if status in {"skipped", "unavailable"} and set(payload) == {"status", "error_code"}:
        code = payload["error_code"]
        if isinstance(code, str) and 1 <= len(code) <= 64:
            return AudioPlaybackResult(played=False, error_code=code)
    if status == "cancelled" and set(payload) == {"status"}:
        return AudioPlaybackResult(played=False, error_code="audio_cancelled")
    return AudioPlaybackResult(played=False, error_code="audio_worker_protocol")
