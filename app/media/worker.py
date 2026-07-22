"""The MediaWorker-side PortAudio handler.

Only this module imports and invokes ``sounddevice``.  It receives WAVs through
the W12 approved-resource descriptor boundary and returns compact state only;
audio frames, paths, and device indexes never cross the helper protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import threading
import wave
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import sounddevice as sd  # type: ignore[import-untyped]

from app.media.stt import WhisperCppConfig, WhisperCppRunner, WhisperRuntimeError
from app.media.types import (
    MAX_OUTPUT_DEVICES,
    AudioOutputDevice,
    clean_device_label,
    output_device_id,
)
from app.windows_security import ReparsePointError, assert_no_reparse_points
from app.workers.access import AuthorizedResource

_PCM_DTYPES = {1: "uint8", 2: "int16", 3: "int24", 4: "int32"}
_MEDIA_PLAY_RESOURCE_ID = "wave"
_PCM_SAMPLE_RATE = 16_000
_PCM_CHANNELS = 1
_PCM_SAMPLE_WIDTH_BYTES = 2
_VOICE_DIRECTORY_PREFIX = "companion-recording-"
_VOICE_ERROR_CAPTURE_OVERFLOW = "stt_capture_overflow"
_VOICE_ERROR_DEVICE_LOST = "stt_capture_device_lost"
_VOICE_ERROR_TOO_LONG = "stt_recording_too_long"
_WIPE_CHUNK = b"\0" * (64 * 1024)


class _OutputStream(Protocol):
    @property
    def active(self) -> bool: ...

    def start(self) -> None: ...

    def write(self, frames: bytes) -> None: ...

    def abort(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class _InputStream(Protocol):
    @property
    def active(self) -> bool: ...

    def start(self) -> None: ...

    def abort(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _DiscoveredOutput:
    device: AudioOutputDevice
    native_index: int


@dataclass(frozen=True, slots=True)
class _WaveFormat:
    sample_rate: int
    channels: int
    dtype: str


class _Backend(Protocol):
    def output_devices(self) -> tuple[_DiscoveredOutput, ...]: ...

    def open_output_stream(
        self,
        audio_format: _WaveFormat,
        *,
        native_index: int,
        latency: str,
    ) -> _OutputStream: ...

    def open_input_stream(
        self,
        *,
        sample_rate: int,
        channels: int,
        device: int | str | None,
        blocksize: int,
        callback: PCMFrameCallback,
    ) -> _InputStream: ...


class PCMFrameCallback(Protocol):
    """Run synchronously on PortAudio's callback thread without loop dispatch."""

    def __call__(self, frame: memoryview, status_present: bool) -> bool: ...


class _MediaWorkerFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SoundDeviceBackend:
    """Thin, injectable adapter around PortAudio's Python binding."""

    def __init__(self, sounddevice: Any = sd) -> None:
        self._sounddevice = sounddevice

    def output_devices(self) -> tuple[_DiscoveredOutput, ...]:
        raw_devices = self._sounddevice.query_devices()
        raw_host_apis = self._sounddevice.query_hostapis()
        default_output = _default_output_index(getattr(self._sounddevice.default, "device", None))
        outputs: list[_DiscoveredOutput] = []
        for native_index, raw in enumerate(raw_devices):
            try:
                channels = int(raw["max_output_channels"])
                if channels < 1:
                    continue
                raw_name = raw["name"]
                host_api_index = int(raw["hostapi"])
                sample_rate = float(raw.get("default_samplerate", 0.0))
                host_api = raw_host_apis[host_api_index]
                host_api_name = str(host_api["name"])
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            if not isinstance(raw_name, str) or not raw_name:
                continue
            outputs.append(
                _DiscoveredOutput(
                    device=AudioOutputDevice(
                        device_id=output_device_id(
                            host_api_name=host_api_name,
                            device_name=raw_name,
                            maximum_output_channels=channels,
                            default_sample_rate=sample_rate,
                        ),
                        label=clean_device_label(raw_name),
                        is_default=native_index == default_output,
                    ),
                    native_index=native_index,
                )
            )
        return tuple(outputs)

    def open_output_stream(
        self,
        audio_format: _WaveFormat,
        *,
        native_index: int,
        latency: str,
    ) -> _OutputStream:
        return cast(
            _OutputStream,
            self._sounddevice.RawOutputStream(
                samplerate=audio_format.sample_rate,
                channels=audio_format.channels,
                dtype=audio_format.dtype,
                device=native_index,
                latency=latency,
            ),
        )

    def open_input_stream(
        self,
        *,
        sample_rate: int,
        channels: int,
        device: int | str | None,
        blocksize: int,
        callback: PCMFrameCallback,
    ) -> _InputStream:
        def on_audio(
            input_data: Any,
            _frame_count: int,
            _time_info: Any,
            status: Any,
        ) -> None:
            # The callback only copies into a preallocated bounded ring.  It
            # never schedules one asyncio callback per audio frame.
            accepted = callback(memoryview(input_data), bool(status))
            if not accepted:
                raise self._sounddevice.CallbackAbort

        return cast(
            _InputStream,
            self._sounddevice.RawInputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
                blocksize=blocksize,
                device=device,
                callback=on_audio,
            ),
        )


class _PCMRecordingRing:
    """A preallocated, bounded PCM ring written entirely in the audio callback.

    This implementation deliberately refuses to overwrite old speech.  A full
    buffer becomes the explicit ``stt_capture_overflow`` terminal condition
    instead of silently truncating the beginning of the user's recording.
    """

    def __init__(self, capacity_bytes: int) -> None:
        if capacity_bytes < _PCM_SAMPLE_WIDTH_BYTES or capacity_bytes % _PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError("voice recording ring capacity invalid")
        self._storage = bytearray(capacity_bytes)
        self._capacity = capacity_bytes
        self._read = 0
        self._write = 0
        self._size = 0
        self._failure_code: str | None = None
        self._lock = threading.Lock()

    @property
    def capacity_bytes(self) -> int:
        return self._capacity

    def append_from_callback(self, frame: memoryview, status_present: bool) -> bool:
        """Copy one frame without allocating or touching an asyncio loop."""

        if status_present:
            self.fail(_VOICE_ERROR_DEVICE_LOST)
            return False
        try:
            raw = frame.cast("B")
        except (TypeError, ValueError):
            self.fail(_VOICE_ERROR_DEVICE_LOST)
            return False
        size = raw.nbytes
        if size == 0:
            return True
        if size % _PCM_SAMPLE_WIDTH_BYTES:
            self.fail(_VOICE_ERROR_DEVICE_LOST)
            return False
        with self._lock:
            if self._failure_code is not None:
                return False
            if size > self._capacity - self._size:
                self._failure_code = _VOICE_ERROR_CAPTURE_OVERFLOW
                _wipe(self._storage)
                self._read = self._write = self._size = 0
                return False
            first = min(size, self._capacity - self._write)
            self._storage[self._write : self._write + first] = raw[:first]
            remaining = size - first
            if remaining:
                self._storage[:remaining] = raw[first:]
            self._write = (self._write + size) % self._capacity
            self._size += size
            return True

    def fail(self, code: str) -> None:
        with self._lock:
            if self._failure_code is None:
                self._failure_code = code
            _wipe(self._storage)
            self._read = self._write = self._size = 0

    def drain(self) -> tuple[bytearray, str | None]:
        """Return the bounded recording and immediately wipe the source ring."""

        with self._lock:
            failure = self._failure_code
            pcm = bytearray(self._size)
            first = min(self._size, self._capacity - self._read)
            pcm[:first] = self._storage[self._read : self._read + first]
            remaining = self._size - first
            if remaining:
                pcm[first:] = self._storage[:remaining]
            _wipe(self._storage)
            self._read = self._write = self._size = 0
            return pcm, failure

    def discard(self) -> None:
        with self._lock:
            _wipe(self._storage)
            self._read = self._write = self._size = 0


@dataclass(slots=True)
class _RecordingSession:
    directory: Path
    ring: _PCMRecordingRing
    stream: _InputStream
    watchdog: asyncio.Task[None]


class MediaWorkerHandler:
    """Own output playback plus private PTT/STT resources in one helper process."""

    def __init__(
        self,
        *,
        backend: _Backend | None = None,
        selected_device_id: str | None = None,
        maximum_wave_bytes: int = 32 * 1024 * 1024,
        stt_config: WhisperCppConfig | None = None,
        recording_root: Path | None = None,
        input_device: int | str | None = None,
        input_blocksize: int = 0,
        maximum_recording_seconds: float = 120.0,
        transcription_timeout_seconds: float = 60.0,
        language: str = "auto",
    ) -> None:
        if maximum_wave_bytes < 44:
            raise ValueError("media worker wave limit is invalid")
        if (stt_config is None) != (recording_root is None):
            raise ValueError("voice worker needs both runtime and private recording root")
        if (
            isinstance(maximum_recording_seconds, bool)
            or not isinstance(maximum_recording_seconds, (int, float))
            or not 0 < maximum_recording_seconds <= 120.0
        ):
            raise ValueError("voice recording maximum must be within 120 seconds")
        if (
            isinstance(input_blocksize, bool)
            or not isinstance(input_blocksize, int)
            or input_blocksize < 0
        ):
            raise ValueError("voice input blocksize invalid")
        if (
            isinstance(transcription_timeout_seconds, bool)
            or not isinstance(transcription_timeout_seconds, (int, float))
            or not 0 < transcription_timeout_seconds <= 120.0
        ):
            raise ValueError("voice transcription timeout must be within 120 seconds")
        if not language.strip() or "\x00" in language or len(language) > 64:
            raise ValueError("voice language invalid")
        self._backend = backend or SoundDeviceBackend()
        self._selected_device_id = selected_device_id or None
        self._maximum_wave_bytes = maximum_wave_bytes
        self._stream: _OutputStream | None = None
        self._stream_key: tuple[_WaveFormat, int, str] | None = None
        self._input_device = input_device
        self._input_blocksize = input_blocksize
        self._maximum_recording_seconds = float(maximum_recording_seconds)
        self._transcription_timeout_seconds = float(transcription_timeout_seconds)
        self._language = language
        self._recording_root = _validated_recording_root(recording_root) if recording_root else None
        self._stt = WhisperCppRunner(stt_config) if stt_config is not None else None
        self._recording: _RecordingSession | None = None
        self._recording_lock = asyncio.Lock()

    async def run_job(
        self,
        job_kind: str,
        resources: tuple[AuthorizedResource, ...],
        cancelled: asyncio.Event,
        *,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        if job_kind == "media.devices":
            return await self._list_devices()
        if job_kind == "media.release":
            if resources:
                return {"status": "skipped", "error_code": "audio_protocol_invalid"}
            return await self._release(immediate=False)
        if job_kind == "media.record_start":
            if resources or job_id is None:
                return _voice_failed("stt_protocol_invalid")
            return await self._start_recording(job_id, cancelled)
        if job_kind == "media.stt_preflight":
            if resources:
                return _voice_failed("stt_protocol_invalid")
            return await self._preflight_stt(cancelled)
        if job_kind == "media.record_stop":
            if resources:
                return _voice_failed("stt_protocol_invalid")
            return await self._stop_recording(cancelled)
        if job_kind == "media.record_cancel":
            if resources:
                return _voice_failed("stt_protocol_invalid")
            await self._cancel_recording()
            return {"status": "cancelled"}
        if job_kind != "media.play":
            return {"status": "skipped", "error_code": "audio_protocol_invalid"}
        if len(resources) != 1 or resources[0].resource_id != _MEDIA_PLAY_RESOURCE_ID:
            return {"status": "skipped", "error_code": "audio_protocol_invalid"}
        return await self._play(resources[0], cancelled)

    async def close(self) -> None:
        await self._cancel_recording()
        stt = self._stt
        if stt is not None:
            await stt.close()
        await self._release(immediate=True)

    async def _start_recording(
        self,
        job_id: str,
        cancelled: asyncio.Event,
    ) -> dict[str, Any]:
        if cancelled.is_set():
            return {"status": "cancelled"}
        stt = self._stt
        root = self._recording_root
        if stt is None or root is None:
            return _voice_failed("stt_disabled")
        try:
            await stt.preflight()
        except WhisperRuntimeError as exc:
            return _voice_failed(exc.code)
        async with self._recording_lock:
            if self._recording is not None:
                return _voice_failed("stt_recording_active")
            directory = _recording_directory(root, job_id)
            ring = _PCMRecordingRing(_recording_capacity(self._maximum_recording_seconds))
            stream: _InputStream | None = None
            try:
                await asyncio.to_thread(_create_recording_directory, root, directory)
                stream = await asyncio.to_thread(
                    self._backend.open_input_stream,
                    sample_rate=_PCM_SAMPLE_RATE,
                    channels=_PCM_CHANNELS,
                    device=self._input_device,
                    blocksize=self._input_blocksize,
                    callback=ring.append_from_callback,
                )
                await asyncio.to_thread(stream.start)
                watchdog = asyncio.create_task(
                    self._watch_recording_limit(ring, stream),
                    name=f"media-recording-limit-{job_id}",
                )
                self._recording = _RecordingSession(
                    directory=directory,
                    ring=ring,
                    stream=stream,
                    watchdog=watchdog,
                )
            except asyncio.CancelledError:
                ring.discard()
                if stream is not None:
                    with suppress(Exception):
                        await asyncio.to_thread(_close_input_stream, stream, True)
                with suppress(Exception):
                    await asyncio.to_thread(_remove_recording_directory, root, directory)
                raise
            except Exception:
                ring.discard()
                if stream is not None:
                    with suppress(Exception):
                        await asyncio.to_thread(_close_input_stream, stream, True)
                with suppress(Exception):
                    await asyncio.to_thread(_remove_recording_directory, root, directory)
                return _voice_failed("stt_capture_start_failed")
        return {"status": "recording"}

    async def _preflight_stt(self, cancelled: asyncio.Event) -> dict[str, Any]:
        if cancelled.is_set():
            return {"status": "cancelled"}
        stt = self._stt
        if stt is None:
            return _voice_failed("stt_disabled")
        try:
            await stt.preflight()
        except WhisperRuntimeError as exc:
            return _voice_failed(exc.code)
        return {"status": "ready"}

    async def _stop_recording(self, cancelled: asyncio.Event) -> dict[str, Any]:
        session = await self._take_recording()
        if session is None:
            return _voice_failed("stt_recording_not_active")
        pcm = bytearray()
        response: dict[str, Any]
        cleanup_succeeded = False
        try:
            await self._stop_session_stream(session, immediate=False)
            pcm, failure = session.ring.drain()
            if cancelled.is_set():
                response = {"status": "cancelled"}
            elif failure is not None:
                response = _voice_failed(failure)
            elif not pcm:
                response = _voice_failed("stt_empty_recording")
            else:
                audio_path = session.directory / "input.wav"
                await asyncio.to_thread(_write_voice_wav, audio_path, pcm)
                stt = self._stt
                if stt is None:
                    response = _voice_failed("stt_disabled")
                else:
                    try:
                        result = await stt.transcribe(
                            audio_path,
                            language=self._language,
                            timeout_seconds=self._transcription_timeout_seconds,
                            cancelled=cancelled,
                        )
                    except WhisperRuntimeError as exc:
                        response = (
                            {"status": "cancelled"}
                            if exc.code == "stt_cancelled"
                            else _voice_failed(exc.code)
                        )
                    else:
                        response = (
                            {"status": "cancelled"}
                            if cancelled.is_set()
                            else {
                                "status": "transcribed",
                                "text": result.text,
                                "language": result.language or "",
                                "segment_count": result.segment_count,
                            }
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            response = _voice_failed("stt_capture_stop_failed")
        finally:
            _wipe(pcm)
            cleanup_succeeded = await self._cleanup_recording_directory(session.directory)
        if not cleanup_succeeded:
            # Do not let a transcript escape as success if raw PCM/WAV/JSON
            # could not be removed. The parent registry retains a retry.
            return _voice_failed("stt_temp_cleanup_pending")
        return response

    async def _cancel_recording(self) -> None:
        session = await self._take_recording()
        if session is None:
            return
        session.ring.discard()
        try:
            await self._stop_session_stream(session, immediate=True)
        finally:
            await self._cleanup_recording_directory(session.directory)

    async def _take_recording(self) -> _RecordingSession | None:
        async with self._recording_lock:
            session, self._recording = self._recording, None
            return session

    async def _stop_session_stream(self, session: _RecordingSession, *, immediate: bool) -> None:
        session.watchdog.cancel()
        await asyncio.gather(session.watchdog, return_exceptions=True)
        await asyncio.to_thread(_close_input_stream, session.stream, immediate)

    async def _watch_recording_limit(
        self,
        ring: _PCMRecordingRing,
        stream: _InputStream,
    ) -> None:
        try:
            await asyncio.sleep(self._maximum_recording_seconds)
        except asyncio.CancelledError:
            raise
        ring.fail(_VOICE_ERROR_TOO_LONG)
        with suppress(Exception):
            await asyncio.to_thread(_close_input_stream, stream, True)

    async def _cleanup_recording_directory(self, directory: Path) -> bool:
        root = self._recording_root
        if root is None:
            return True
        try:
            await asyncio.to_thread(_remove_recording_directory, root, directory)
        except Exception:
            return False
        return True

    async def _list_devices(self) -> dict[str, Any]:
        try:
            discovered = await asyncio.to_thread(self._backend.output_devices)
        except Exception:
            return {"status": "unavailable", "devices": [], "truncated": False}
        selectable, _duplicates = _selectable_outputs(discovered)
        listed = sorted(
            selectable,
            key=lambda item: (
                not item.device.is_default,
                item.device.label.casefold(),
                item.device.device_id,
            ),
        )
        truncated = len(listed) > MAX_OUTPUT_DEVICES
        return {
            "status": "ok",
            "devices": [
                {
                    "device_id": item.device.device_id,
                    "label": item.device.label,
                    "is_default": item.device.is_default,
                }
                for item in listed[:MAX_OUTPUT_DEVICES]
            ],
            "truncated": truncated,
        }

    async def _play(
        self,
        resource: AuthorizedResource,
        cancelled: asyncio.Event,
    ) -> dict[str, Any]:
        if cancelled.is_set():
            return {"status": "cancelled"}
        try:
            audio_format, frames = await asyncio.to_thread(
                _read_pcm_wave,
                resource.value,
                self._maximum_wave_bytes,
            )
            discovered = await asyncio.to_thread(self._backend.output_devices)
            target, device_fallback = _choose_output(discovered, self._selected_device_id)
            stream, latency_fallback = await asyncio.to_thread(
                self._ensure_stream,
                audio_format,
                target.native_index,
            )
        except _MediaWorkerFailure as exc:
            return {"status": "skipped", "error_code": exc.code}
        except Exception:
            return {"status": "skipped", "error_code": "audio_device_unavailable"}

        write_task = asyncio.create_task(asyncio.to_thread(stream.write, frames))
        cancellation = asyncio.create_task(cancelled.wait())
        try:
            done, _pending = await asyncio.wait(
                {write_task, cancellation},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done:
                await asyncio.to_thread(_abort_stream, stream)
                await asyncio.gather(write_task, return_exceptions=True)
                await asyncio.to_thread(self._drop_stream, stream, True)
                return {"status": "cancelled"}
            try:
                await write_task
            except Exception:
                await asyncio.to_thread(self._drop_stream, stream, True)
                return {"status": "skipped", "error_code": "audio_device_lost"}
            if cancelled.is_set():
                await asyncio.to_thread(self._drop_stream, stream, True)
                return {"status": "cancelled"}
        except asyncio.CancelledError:
            await asyncio.to_thread(_abort_stream, stream)
            await asyncio.gather(write_task, return_exceptions=True)
            await asyncio.to_thread(self._drop_stream, stream, True)
            raise
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)

        notice_code = (
            "audio_device_fallback"
            if device_fallback
            else "audio_latency_fallback"
            if latency_fallback
            else ""
        )
        return {"status": "played", "notice_code": notice_code}

    async def _release(self, *, immediate: bool) -> dict[str, Any]:
        try:
            await asyncio.to_thread(self._close_stream, immediate)
        except Exception:
            return {"status": "skipped", "error_code": "audio_device_release_failed"}
        return {"status": "released"}

    def _ensure_stream(
        self,
        audio_format: _WaveFormat,
        native_index: int,
    ) -> tuple[_OutputStream, bool]:
        low_key = (audio_format, native_index, "low")
        high_key = (audio_format, native_index, "high")
        stream = self._stream
        if stream is not None and self._stream_key in {low_key, high_key} and stream.active:
            return stream, self._stream_key == high_key
        self._close_stream(immediate=False)
        low_latency_stream: _OutputStream | None = None
        try:
            low_latency_stream = self._backend.open_output_stream(
                audio_format,
                native_index=native_index,
                latency="low",
            )
            low_latency_stream.start()
        except Exception:
            _close_unowned_stream(low_latency_stream)
            high_latency_stream: _OutputStream | None = None
            try:
                high_latency_stream = self._backend.open_output_stream(
                    audio_format,
                    native_index=native_index,
                    latency="high",
                )
                high_latency_stream.start()
            except Exception as exc:
                _close_unowned_stream(high_latency_stream)
                raise _MediaWorkerFailure("audio_device_unavailable") from exc
            assert high_latency_stream is not None
            self._stream = high_latency_stream
            self._stream_key = high_key
            return high_latency_stream, True
        assert low_latency_stream is not None
        self._stream = low_latency_stream
        self._stream_key = low_key
        return low_latency_stream, False

    def _drop_stream(self, stream: _OutputStream, immediate: bool) -> None:
        if self._stream is stream:
            self._close_stream(immediate=immediate)
            return
        _close_unowned_stream(stream, immediate=immediate)

    def _close_stream(self, immediate: bool) -> None:
        stream = self._stream
        self._stream = None
        self._stream_key = None
        _close_unowned_stream(stream, immediate=immediate)


def _voice_failed(code: str) -> dict[str, str]:
    return {"status": "failed", "error_code": code}


def _recording_capacity(seconds: float) -> int:
    capacity = round(seconds * _PCM_SAMPLE_RATE * _PCM_CHANNELS * _PCM_SAMPLE_WIDTH_BYTES)
    return capacity - (capacity % _PCM_SAMPLE_WIDTH_BYTES)


def _recording_directory(root: Path, job_id: str) -> Path:
    digest = hashlib.sha256(job_id.encode("ascii")).hexdigest()[:32]
    return root / f"{_VOICE_DIRECTORY_PREFIX}{digest}"


def _validated_recording_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise OSError("recording root is not a directory")
        assert_no_reparse_points(resolved, resolved)
        return resolved
    except (OSError, ReparsePointError) as exc:
        raise ValueError("voice recording root invalid") from exc


def _create_recording_directory(root: Path, directory: Path) -> None:
    try:
        directory.relative_to(root)
        assert_no_reparse_points(root, directory.parent)
        directory.mkdir(mode=0o700)
        assert_no_reparse_points(root, directory)
    except (OSError, ValueError, ReparsePointError) as exc:
        raise _MediaWorkerFailure("stt_recording_directory_failed") from exc


def _remove_recording_directory(root: Path, directory: Path) -> None:
    try:
        directory.relative_to(root)
        if directory.exists():
            assert_no_reparse_points(root, directory)
            shutil.rmtree(directory)
    except (OSError, ValueError, ReparsePointError) as exc:
        raise _MediaWorkerFailure("stt_temp_cleanup_pending") from exc


def _write_voice_wav(path: Path, pcm: bytearray) -> None:
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(_PCM_CHANNELS)
        recording.setsampwidth(_PCM_SAMPLE_WIDTH_BYTES)
        recording.setframerate(_PCM_SAMPLE_RATE)
        recording.writeframes(pcm)


def _close_input_stream(stream: _InputStream, immediate: bool) -> None:
    try:
        if stream.active:
            if immediate:
                stream.abort()
            else:
                stream.stop()
    finally:
        stream.close()


def _wipe(buffer: bytearray) -> None:
    # Do not allocate another multi-megabyte bytes object from PortAudio's
    # callback path.  The only buffer whose capacity is proportional to the
    # recording is allocated once in ``_PCMRecordingRing.__init__``.
    for offset in range(0, len(buffer), len(_WIPE_CHUNK)):
        buffer[offset : offset + len(_WIPE_CHUNK)] = _WIPE_CHUNK[: len(buffer) - offset]


def _default_output_index(value: object) -> int | None:
    if isinstance(value, Sequence) and not isinstance(value, str) and len(value) >= 2:
        candidate = value[1]
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    return None


def _selectable_outputs(
    discovered: Sequence[_DiscoveredOutput],
) -> tuple[tuple[_DiscoveredOutput, ...], frozenset[str]]:
    counts: dict[str, int] = {}
    for item in discovered:
        counts[item.device.device_id] = counts.get(item.device.device_id, 0) + 1
    duplicate_ids = frozenset(key for key, count in counts.items() if count > 1)
    return (
        tuple(item for item in discovered if item.device.device_id not in duplicate_ids),
        duplicate_ids,
    )


def _choose_output(
    discovered: Sequence[_DiscoveredOutput],
    selected_device_id: str | None,
) -> tuple[_DiscoveredOutput, bool]:
    selectable, _duplicates = _selectable_outputs(discovered)
    if not selectable:
        raise _MediaWorkerFailure("audio_device_unavailable")
    if selected_device_id:
        selected = next(
            (item for item in selectable if item.device.device_id == selected_device_id),
            None,
        )
        if selected is not None:
            return selected, False
    default = next((item for item in selectable if item.device.is_default), None)
    return default or selectable[0], bool(selected_device_id)


def _read_pcm_wave(descriptor: int, maximum_bytes: int) -> tuple[_WaveFormat, bytes]:
    try:
        if os.fstat(descriptor).st_size > maximum_bytes:
            raise _MediaWorkerFailure("audio_result_too_large")
        duplicate = os.dup(descriptor)
        # ``dup`` shares the file-description cursor with the approved
        # descriptor.  Seek the private duplicate before parsing so a reused
        # inherited lease cannot turn a later playback into an EOF failure.
        os.lseek(duplicate, 0, os.SEEK_SET)
        with os.fdopen(duplicate, "rb") as stream, wave.open(stream, "rb") as audio:
            sample_width = audio.getsampwidth()
            dtype = _PCM_DTYPES.get(sample_width)
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
            if (
                audio.getcomptype() != "NONE"
                or dtype is None
                or channels < 1
                or sample_rate < 1
                or frame_count < 1
                or frame_count * channels * sample_width > maximum_bytes
            ):
                raise _MediaWorkerFailure("audio_wave_invalid")
            frames = audio.readframes(frame_count)
    except _MediaWorkerFailure:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise _MediaWorkerFailure("audio_wave_invalid") from exc
    if len(frames) != frame_count * channels * sample_width:
        raise _MediaWorkerFailure("audio_wave_invalid")
    return _WaveFormat(sample_rate=sample_rate, channels=channels, dtype=dtype), frames


def _abort_stream(stream: _OutputStream) -> None:
    try:
        if stream.active:
            stream.abort()
    except Exception:
        return


def _close_unowned_stream(stream: _OutputStream | None, *, immediate: bool = False) -> None:
    if stream is None:
        return
    try:
        if stream.active:
            if immediate:
                stream.abort()
            else:
                stream.stop()
    finally:
        stream.close()
