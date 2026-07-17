"""Generate deterministic, legal PCM WAV tones without voice or character assets."""

from __future__ import annotations

import asyncio
import hashlib
import math
import struct
import wave
from contextlib import suppress
from pathlib import Path

from app.core.cancellation import CancellationToken
from app.schemas import AudioResult, TTSJob
from app.temp_assets import TempAssetKind, TempAssetRegistry


class MockTTSProvider:
    def __init__(
        self,
        cache_directory: Path,
        *,
        sample_rate: int = 48_000,
        duration_ms: int = 180,
        volume: float = 0.12,
        synthesis_delay_seconds: float = 0.01,
        delay_by_index: dict[int, float] | None = None,
        temp_registry: TempAssetRegistry | None = None,
    ) -> None:
        if sample_rate <= 0 or duration_ms < 0:
            raise ValueError("sample_rate 必须为正且 duration_ms 不能为负")
        if not 0.0 <= volume <= 1.0:
            raise ValueError("volume 必须位于 0.0 到 1.0")
        if synthesis_delay_seconds < 0:
            raise ValueError("合成延迟不能为负数")
        self._cache_directory = cache_directory
        self._sample_rate = sample_rate
        self._duration_ms = duration_ms
        self._volume = volume
        self._synthesis_delay = synthesis_delay_seconds
        self._delay_by_index = delay_by_index or {}
        self._temp_registry = temp_registry
        self._paths: set[Path] = set()
        self._asset_ids: dict[Path, str] = {}

    async def synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult:
        delay = self._delay_by_index.get(segment_index, self._synthesis_delay)
        if await token.wait_or_timeout(delay):
            token.raise_if_cancelled()
        token.raise_if_cancelled()

        turn_key = hashlib.sha256(job.turn_id.encode()).hexdigest()[:16]
        job_key = hashlib.sha256(job.job_id.encode()).hexdigest()[:20]
        directory = self._cache_directory / turn_key
        path = directory / f"{segment_index:04d}-{job_key}.wav"
        frequency_hz = 440.0 + (segment_index % 6) * 110.0
        # The file is tiny and generated synchronously so cancellation cannot leave a
        # background writer racing with cleanup on Windows.
        if self._temp_registry is not None:
            entry = self._temp_registry.register(path, TempAssetKind.mock_wav)
            self._asset_ids[path] = entry.asset_id
        try:
            self._write_wave(path, frequency_hz)
        except BaseException:
            await self._delete_path(path)
            raise
        self._paths.add(path)
        if token.cancelled:
            self._paths.discard(path)
            await self._delete_path(path)
            self._remove_empty_parent(path.parent)
            token.raise_if_cancelled()
        return AudioResult(
            job_id=job.job_id,
            turn_id=job.turn_id,
            segment_id=job.segment_id,
            success=True,
            audio_path=path,
            sample_rate=self._sample_rate,
            duration_ms=self._duration_ms,
        )

    def _write_wave(self, path: Path, frequency_hz: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame_count = round(self._sample_rate * self._duration_ms / 1000)
        amplitude = round(32767 * self._volume)
        frames = bytearray()
        for frame_index in range(frame_count):
            phase = 2.0 * math.pi * frequency_hz * frame_index / self._sample_rate
            sample = round(amplitude * math.sin(phase))
            frames.extend(struct.pack("<h", sample))
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(self._sample_rate)
            output.writeframes(frames)

    async def discard(self, result: AudioResult) -> None:
        if result.audio_path is None:
            return
        path = result.audio_path
        self._paths.discard(path)
        await self._delete_path(path)
        await asyncio.to_thread(self._remove_empty_parent, path.parent)

    async def close(self) -> None:
        paths = tuple(self._paths)
        self._paths.clear()
        await asyncio.gather(*(self._delete_path(path) for path in paths))
        parents = {path.parent for path in paths}
        await asyncio.gather(
            *(asyncio.to_thread(self._remove_empty_parent, parent) for parent in parents)
        )

    def _remove_empty_parent(self, parent: Path) -> None:
        with suppress(FileNotFoundError, OSError):
            parent.rmdir()

    async def _delete_path(self, path: Path) -> None:
        asset_id = self._asset_ids.pop(path, None)
        if self._temp_registry is not None and asset_id is not None:
            await asyncio.to_thread(
                self._temp_registry.delete,
                asset_id,
                ignore_retry_deadline=True,
            )
            return
        await asyncio.to_thread(path.unlink, missing_ok=True)
