"""The MediaWorker-side PortAudio handler.

Only this module imports and invokes ``sounddevice``.  It receives WAVs through
the W12 approved-resource descriptor boundary and returns compact state only;
audio frames, paths, and device indexes never cross the helper protocol.
"""

from __future__ import annotations

import asyncio
import os
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import sounddevice as sd  # type: ignore[import-untyped]

from app.media.types import (
    MAX_OUTPUT_DEVICES,
    AudioOutputDevice,
    clean_device_label,
    output_device_id,
)
from app.workers.access import AuthorizedResource

_PCM_DTYPES = {1: "uint8", 2: "int16", 3: "int24", 4: "int32"}
_MEDIA_PLAY_RESOURCE_ID = "wave"


class _OutputStream(Protocol):
    @property
    def active(self) -> bool: ...

    def start(self) -> None: ...

    def write(self, frames: bytes) -> None: ...

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


class MediaWorkerHandler:
    """Own one output stream and convert native failures into stable results."""

    def __init__(
        self,
        *,
        backend: _Backend | None = None,
        selected_device_id: str | None = None,
        maximum_wave_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        if maximum_wave_bytes < 44:
            raise ValueError("media worker wave limit is invalid")
        self._backend = backend or SoundDeviceBackend()
        self._selected_device_id = selected_device_id or None
        self._maximum_wave_bytes = maximum_wave_bytes
        self._stream: _OutputStream | None = None
        self._stream_key: tuple[_WaveFormat, int, str] | None = None

    async def run_job(
        self,
        job_kind: str,
        resources: tuple[AuthorizedResource, ...],
        cancelled: asyncio.Event,
    ) -> dict[str, Any]:
        if job_kind == "media.devices":
            return await self._list_devices()
        if job_kind == "media.release":
            if resources:
                return {"status": "skipped", "error_code": "audio_protocol_invalid"}
            return await self._release(immediate=False)
        if job_kind != "media.play":
            return {"status": "skipped", "error_code": "audio_protocol_invalid"}
        if len(resources) != 1 or resources[0].resource_id != _MEDIA_PLAY_RESOURCE_ID:
            return {"status": "skipped", "error_code": "audio_protocol_invalid"}
        return await self._play(resources[0], cancelled)

    async def close(self) -> None:
        await self._release(immediate=True)

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
