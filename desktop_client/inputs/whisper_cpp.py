"""Cancellation-safe ``whisper-cli`` subprocess adapter.

The adapter never invokes a shell, never reads transcript text from stdout or
stderr, and confines whisper's JSON output to an automatically removed
directory.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import wave
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.temp_assets import TempAssetKind, TempAssetRegistry

from desktop_client.inputs.stt_contracts import (
    STTError,
    STTErrorCode,
    TranscriptionRequest,
    TranscriptionResult,
)


@dataclass(frozen=True, slots=True)
class WhisperCppConfig:
    executable: Path
    model_path: Path
    executable_prefix_args: tuple[str, ...] = ()
    threads: int | None = None
    temporary_directory: Path | None = None
    terminate_grace_seconds: float = 0.5
    max_audio_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.threads is not None and self.threads < 1:
            raise ValueError("threads 必须大于 0")
        if self.terminate_grace_seconds <= 0:
            raise ValueError("terminate_grace_seconds 必须大于 0")
        if self.max_audio_bytes < 44 or self.max_output_bytes < 1:
            raise ValueError("音频和输出大小上限无效")


class WhisperCppProvider:
    """Run one isolated whisper-cli process per completed recording."""

    def __init__(
        self,
        config: WhisperCppConfig,
        *,
        temp_registry: TempAssetRegistry | None = None,
    ) -> None:
        self._config = config
        self._temp_registry = temp_registry
        self._processes: set[asyncio.subprocess.Process] = set()
        self._termination_tasks: dict[asyncio.subprocess.Process, asyncio.Task[None]] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self._validate_preconditions(request.audio_path)
        temporary_root = self._config.temporary_directory
        if temporary_root is None:
            with tempfile.TemporaryDirectory(prefix="companion-stt-") as directory_name:
                return await self._transcribe_in_directory(request, Path(directory_name))
        directory = temporary_root / f"companion-stt-{uuid4().hex}"
        asset_id: str | None = None
        registry = self._temp_registry
        if registry is not None:
            entry = await asyncio.to_thread(
                registry.register,
                directory,
                TempAssetKind.stt_directory,
            )
            asset_id = entry.asset_id
        directory.mkdir(parents=True)
        try:
            return await self._transcribe_in_directory(request, directory)
        finally:
            if registry is not None and asset_id is not None:
                await asyncio.to_thread(
                    registry.delete,
                    asset_id,
                    ignore_retry_deadline=True,
                )
            else:
                await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)

    async def _transcribe_in_directory(
        self,
        request: TranscriptionRequest,
        directory: Path,
    ) -> TranscriptionResult:
        output_base = directory / "transcript"
        command = self._build_command(request, output_base)
        process = await self._start_process(command)
        try:
            await self._wait_for_process(process, request.timeout_seconds)
            if process.returncode != 0:
                raise STTError(
                    STTErrorCode.process_failed,
                    f"whisper-cli 退出码为 {process.returncode}",
                )
            return self._read_result(output_base.with_suffix(".json"))
        finally:
            await self._terminate(process)

    def _validate_preconditions(self, audio_path: Path) -> None:
        if self._closed:
            raise STTError(STTErrorCode.closed, "STT provider 已关闭")
        if not self._config.executable.is_file():
            raise STTError(STTErrorCode.executable_missing, "whisper-cli 不存在")
        if not self._config.model_path.is_file():
            raise STTError(STTErrorCode.model_missing, "Whisper 模型不存在")
        if not audio_path.is_file() or audio_path.stat().st_size > self._config.max_audio_bytes:
            raise STTError(STTErrorCode.invalid_audio, "录音文件不存在或大小无效")
        try:
            with wave.open(str(audio_path), "rb") as recording:
                is_valid = (
                    recording.getnchannels() == 1
                    and recording.getsampwidth() == 2
                    and recording.getframerate() == 16_000
                    and recording.getnframes() > 0
                    and recording.getcomptype() == "NONE"
                )
        except (OSError, EOFError, wave.Error) as exc:
            raise STTError(STTErrorCode.invalid_audio, "录音不是有效 PCM WAV") from exc
        if not is_valid:
            raise STTError(STTErrorCode.invalid_audio, "录音必须为 16kHz 单声道 16-bit PCM WAV")

    def _build_command(self, request: TranscriptionRequest, output_base: Path) -> tuple[str, ...]:
        command = [
            str(self._config.executable),
            *self._config.executable_prefix_args,
            "--model",
            str(self._config.model_path),
            "--file",
            str(request.audio_path),
            "--language",
            request.language,
            "--output-json",
            "--output-file",
            str(output_base),
            "--no-prints",
        ]
        if self._config.threads is not None:
            command.extend(("--threads", str(self._config.threads)))
        return tuple(command)

    async def _start_process(self, command: tuple[str, ...]) -> asyncio.subprocess.Process:
        async with self._lifecycle_lock:
            if self._closed:
                raise STTError(STTErrorCode.closed, "STT provider 已关闭")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                raise STTError(STTErrorCode.process_start_failed, "无法启动 whisper-cli") from exc
            self._processes.add(process)
            return process

    async def _wait_for_process(
        self, process: asyncio.subprocess.Process, timeout_seconds: float
    ) -> None:
        try:
            async with asyncio.timeout(timeout_seconds):
                await process.wait()
        except TimeoutError as exc:
            await self._terminate(process)
            raise STTError(STTErrorCode.timeout, "whisper-cli 转写超时") from exc
        except asyncio.CancelledError:
            await self._terminate(process)
            raise

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        async with self._lifecycle_lock:
            task = self._termination_tasks.get(process)
            if task is None:
                task = asyncio.create_task(
                    self._terminate_once(process),
                    name=f"whisper-process-reaper-{process.pid}",
                )
                self._termination_tasks[process] = task
        await _await_cleanup_task(task)

    async def _terminate_once(self, process: asyncio.subprocess.Process) -> None:
        current = asyncio.current_task()
        assert current is not None
        try:
            await self._terminate_process(process)
        finally:
            async with self._lifecycle_lock:
                self._processes.discard(process)
                if self._termination_tasks.get(process) is current:
                    self._termination_tasks.pop(process, None)

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        try:
            process.terminate()
        except ProcessLookupError:
            await process.wait()
            return
        try:
            async with asyncio.timeout(self._config.terminate_grace_seconds):
                await process.wait()
                return
        except TimeoutError:
            pass
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()

    def _read_result(self, output_path: Path) -> TranscriptionResult:
        if not output_path.is_file():
            raise STTError(STTErrorCode.invalid_response, "whisper-cli 未生成 JSON 结果")
        if output_path.stat().st_size > self._config.max_output_bytes:
            raise STTError(STTErrorCode.output_too_large, "whisper-cli JSON 结果过大")
        try:
            payload: Any = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise STTError(STTErrorCode.invalid_response, "whisper-cli JSON 结果无效") from exc
        if not isinstance(payload, dict):
            raise STTError(STTErrorCode.invalid_response, "whisper-cli JSON 顶层必须为对象")
        raw_segments = payload.get("transcription")
        if not isinstance(raw_segments, list):
            raise STTError(STTErrorCode.invalid_response, "whisper-cli JSON 缺少 transcription")
        texts: list[str] = []
        for segment in raw_segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                raise STTError(STTErrorCode.invalid_response, "whisper-cli segment 格式无效")
            if normalized := segment["text"].strip():
                texts.append(normalized)
        if not texts:
            raise STTError(STTErrorCode.empty_transcript, "录音中没有可用语音")
        raw_result = payload.get("result")
        language = raw_result.get("language") if isinstance(raw_result, dict) else None
        if not isinstance(language, str):
            language = None
        return TranscriptionResult(
            text=" ".join(texts),
            language=language,
            segment_count=len(raw_segments),
        )

    async def close(self) -> None:
        async with self._lifecycle_lock:
            task = self._close_task
            if task is None:
                self._closed = True
                task = asyncio.create_task(self._close_impl(), name="whisper-provider-close")
                self._close_task = task
        await asyncio.shield(task)

    async def _close_impl(self) -> None:
        while True:
            async with self._lifecycle_lock:
                processes = tuple(self._processes)
                cleanup = tuple(self._termination_tasks.values())
            if not processes and not cleanup:
                return
            results = await asyncio.gather(
                *(self._terminate(process) for process in processes),
                *(_await_cleanup_task(task) for task in cleanup),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, Exception)]
            if errors:
                raise ExceptionGroup("whisper process cleanup failed", errors)


async def _await_cleanup_task(task: asyncio.Task[None]) -> None:
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            if cancelled:
                raise asyncio.CancelledError
            return
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                raise
