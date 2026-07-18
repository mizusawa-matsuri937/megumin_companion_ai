"""Generate deterministic, legal PCM WAV tones without voice or character assets."""

from __future__ import annotations

import asyncio
import hashlib
import math
import struct
import wave
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

from app.core.cancellation import CancellationToken
from app.schemas import AudioResult, TTSJob
from app.temp_assets import TempAssetKind, TempAssetRegistry

_ResultT = TypeVar("_ResultT")


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
        self._filesystem_lock = asyncio.Lock()

    async def synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult:
        deadline = asyncio.get_running_loop().time() + job.timeout_ms / 1000
        delay = self._delay_by_index.get(segment_index, self._synthesis_delay)
        try:
            async with asyncio.timeout_at(deadline):
                if await token.wait_or_timeout(delay):
                    token.raise_if_cancelled()
        except TimeoutError:
            return self._failure(job, "tts_total_timeout")
        token.raise_if_cancelled()

        turn_key = hashlib.sha256(job.turn_id.encode()).hexdigest()[:16]
        job_key = hashlib.sha256(job.job_id.encode()).hexdigest()[:20]
        directory = self._cache_directory / turn_key
        path = directory / f"{segment_index:04d}-{job_key}.wav"
        frequency_hz = 440.0 + (segment_index % 6) * 110.0
        lock_acquired = False
        try:
            async with asyncio.timeout_at(deadline):
                await self._filesystem_lock.acquire()
                lock_acquired = True
        except TimeoutError:
            if lock_acquired:
                self._filesystem_lock.release()
            return self._failure(job, "tts_total_timeout")
        try:
            token.raise_if_cancelled()
            if self._temp_registry is not None:
                entry = self._temp_registry.register(path, TempAssetKind.mock_wav)
                self._asset_ids[path] = entry.asset_id
            try:
                self._raise_if_expired(deadline)
                await _run_owned_thread(
                    self._write_wave,
                    path,
                    frequency_hz,
                    token=token,
                    deadline=deadline,
                )
                await _run_owned_thread(
                    _validate_wave,
                    path,
                    self._sample_rate,
                    round(self._sample_rate * self._duration_ms / 1000),
                    token=token,
                    deadline=deadline,
                )
                self._raise_if_expired(deadline)
                token.raise_if_cancelled()
            except TimeoutError:
                await self._cleanup_failed_path_locked(path)
                return self._failure(job, "tts_total_timeout")
            except BaseException:
                await self._cleanup_failed_path_locked(path)
                raise
            self._paths.add(path)
        finally:
            self._filesystem_lock.release()
        return AudioResult(
            job_id=job.job_id,
            turn_id=job.turn_id,
            segment_id=job.segment_id,
            success=True,
            audio_path=path,
            sample_rate=self._sample_rate,
            duration_ms=self._duration_ms,
        )

    @staticmethod
    def _failure(job: TTSJob, error_code: str) -> AudioResult:
        return AudioResult(
            job_id=job.job_id,
            turn_id=job.turn_id,
            segment_id=job.segment_id,
            success=False,
            error_code=error_code,
        )

    @staticmethod
    def _raise_if_expired(deadline: float) -> None:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError

    async def _cleanup_failed_path_locked(self, path: Path) -> None:
        self._paths.discard(path)
        await self._delete_path(path)
        await asyncio.to_thread(self._remove_empty_parent, path.parent)

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
        async with self._filesystem_lock:
            self._paths.discard(path)
            await self._delete_path(path)
            await asyncio.to_thread(self._remove_empty_parent, path.parent)

    async def close(self) -> None:
        async with self._filesystem_lock:
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


async def _run_owned_thread(
    function: Callable[..., _ResultT],
    /,
    *args: object,
    token: CancellationToken,
    deadline: float,
) -> _ResultT:
    """Run one local file operation without leaving a thread after return."""

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    cancellation = asyncio.create_task(token.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation, cancellation},
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in done:
            return operation.result()
        await _finish_owned(operation)
        if cancellation in done:
            token.raise_if_cancelled()
        raise TimeoutError
    except asyncio.CancelledError:
        await _finish_owned(operation)
        raise
    finally:
        cancellation.cancel()
        await asyncio.gather(cancellation, return_exceptions=True)


async def _finish_owned(task: asyncio.Task[_ResultT]) -> _ResultT:
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


def _validate_wave(path: Path, sample_rate: int, frame_count: int) -> None:
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getframerate() != sample_rate
                or audio.getnframes() != frame_count
                or audio.getcomptype() != "NONE"
            ):
                raise ValueError("mock wav validation failed")
    except (EOFError, OSError, wave.Error) as exc:
        raise ValueError("mock wav validation failed") from exc
