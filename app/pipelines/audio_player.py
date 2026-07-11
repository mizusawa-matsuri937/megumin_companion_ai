"""Ordered-playback backends for automated tests and manual Gate A review."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any, Protocol

import sounddevice as sd  # type: ignore[import-untyped]

from app.core.cancellation import CancellationToken, TurnCancelledError
from app.schemas import AudioResult


class AudioPlayer(Protocol):
    async def play(self, result: AudioResult, token: CancellationToken) -> None: ...

    async def stop(self, *, immediate: bool = False) -> None: ...

    async def close(self) -> None: ...


class SilentAudioPlayer:
    """Model playback duration without opening an audio device."""

    def __init__(self, *, realtime: bool = False) -> None:
        self._realtime = realtime

    async def play(self, result: AudioResult, token: CancellationToken) -> None:
        duration = (result.duration_ms or 0) / 1000 if self._realtime else 0.0
        if await token.wait_or_timeout(duration):
            token.raise_if_cancelled()

    async def stop(self, *, immediate: bool = False) -> None:
        return None

    async def close(self) -> None:
        return None


class SystemAudioPlayer:
    """Reuse one low-latency output stream across segments and release it on close."""

    def __init__(self) -> None:
        self._stream: Any | None = None
        self._format: tuple[int, int, str] | None = None
        self._lock = asyncio.Lock()

    async def play(self, result: AudioResult, token: CancellationToken) -> None:
        if not result.success or result.audio_path is None:
            return
        audio_format, frames = await asyncio.to_thread(self._read_wave, result.audio_path)
        async with self._lock:
            token.raise_if_cancelled()
            stream = await asyncio.to_thread(self._ensure_stream, audio_format)
            write_task = asyncio.create_task(asyncio.to_thread(stream.write, frames))
            cancel_task = asyncio.create_task(token.wait())
            try:
                done, _pending = await asyncio.wait(
                    {write_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancel_task in done:
                    await asyncio.to_thread(stream.abort)
                    await asyncio.gather(write_task, return_exceptions=True)
                    raise TurnCancelledError
                await write_task
            except asyncio.CancelledError:
                if not write_task.done():
                    await asyncio.to_thread(stream.abort)
                await asyncio.gather(write_task, return_exceptions=True)
                raise
            finally:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

    def _read_wave(self, path: Path) -> tuple[tuple[int, int, str], bytes]:
        with wave.open(str(path), "rb") as audio:
            if audio.getcomptype() != "NONE":
                raise RuntimeError("只支持未压缩 PCM WAV")
            dtype_by_width = {1: "uint8", 2: "int16", 3: "int24", 4: "int32"}
            sample_width = audio.getsampwidth()
            dtype = dtype_by_width.get(sample_width)
            if dtype is None:
                raise RuntimeError(f"不支持 {sample_width} 字节采样宽度的 WAV")
            audio_format = (audio.getframerate(), audio.getnchannels(), dtype)
            frames = audio.readframes(audio.getnframes())
        return audio_format, frames

    def _ensure_stream(self, audio_format: tuple[int, int, str]) -> Any:
        if self._stream is not None and self._format != audio_format:
            self._close_stream(immediate=False)
        if self._stream is None:
            sample_rate, channels, dtype = audio_format
            self._stream = sd.RawOutputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype=dtype,
                latency="low",
            )
            self._format = audio_format
        if not self._stream.active:
            self._stream.start()
        return self._stream

    def _close_stream(self, *, immediate: bool) -> None:
        stream = self._stream
        self._stream = None
        self._format = None
        if stream is None:
            return
        try:
            if stream.active:
                if immediate:
                    stream.abort()
                else:
                    # PortAudio stop drains queued frames; abort intentionally drops them.
                    stream.stop()
        finally:
            stream.close()

    async def stop(self, *, immediate: bool = False) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close_stream, immediate=immediate)

    async def close(self) -> None:
        await self.stop()
