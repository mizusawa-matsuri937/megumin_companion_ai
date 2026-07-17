"""Explicit push-to-talk recording state machine.

No audio device is opened until :meth:`PushToTalkRecorder.start` is called.
Raw PCM remains in memory while recording and in a private temporary WAV only
while the local STT provider is running.
"""

from __future__ import annotations

import asyncio
import importlib
import shutil
import tempfile
import wave
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.core.contracts import UserMessageSink
from app.schemas import InputMode, TurnState, UserMessage
from app.temp_assets import TempAssetKind, TempAssetRegistry

from desktop_client.inputs.stt_contracts import (
    STTProvider,
    TranscriptionRequest,
    TranscriptionResult,
)

PCMCallback = Callable[[bytes], None]


class RecordingState(StrEnum):
    idle = "idle"
    recording = "recording"
    timed_out = "timed_out"
    transcribing = "transcribing"
    closed = "closed"


class VoiceInputErrorCode(StrEnum):
    invalid_state = "voice_invalid_state"
    capture_failed = "voice_capture_failed"
    recording_too_long = "voice_recording_too_long"
    empty_recording = "voice_empty_recording"


class VoiceInputError(RuntimeError):
    def __init__(self, code: VoiceInputErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PCMInputSource(Protocol):
    async def start(self, callback: PCMCallback, *, sample_rate: int, channels: int) -> None: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...


class SoundDevicePCMInput:
    """Lazy sounddevice adapter; importing this module never requests mic access."""

    def __init__(self, *, device: int | str | None = None, blocksize: int = 0) -> None:
        self._device = device
        self._blocksize = blocksize
        self._stream: Any | None = None

    async def start(self, callback: PCMCallback, *, sample_rate: int, channels: int) -> None:
        if self._stream is not None:
            raise VoiceInputError(VoiceInputErrorCode.invalid_state, "录音设备已经启动")
        try:
            sounddevice: Any = importlib.import_module("sounddevice")

            def on_audio(
                input_data: Any,
                _frame_count: int,
                _time_info: Any,
                status: Any,
            ) -> None:
                if status:
                    return
                callback(bytes(input_data))

            stream = sounddevice.RawInputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
                blocksize=self._blocksize,
                device=self._device,
                callback=on_audio,
            )
            await _drainable_to_thread(stream.start)
        except BaseException as exc:
            if "stream" in locals():
                await _drainable_to_thread(stream.close)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise VoiceInputError(
                VoiceInputErrorCode.capture_failed, "无法启动本地麦克风录音"
            ) from exc
        self._stream = stream

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            await _drainable_to_thread(stream.stop)
        finally:
            await _drainable_to_thread(stream.close)

    async def close(self) -> None:
        await self.stop()


@dataclass(frozen=True, slots=True)
class PushToTalkConfig:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width_bytes: int = 2
    max_recording_seconds: float = 120.0
    transcription_timeout_seconds: float = 60.0
    language: str = "auto"
    temporary_directory: Path | None = None

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000 or self.channels != 1 or self.sample_width_bytes != 2:
            raise ValueError("首版录音格式固定为 16kHz 单声道 16-bit PCM")
        if self.max_recording_seconds <= 0 or self.transcription_timeout_seconds <= 0:
            raise ValueError("录音和转写超时必须大于 0")

    @property
    def max_pcm_bytes(self) -> int:
        raw_size = round(
            self.sample_rate * self.channels * self.sample_width_bytes * self.max_recording_seconds
        )
        frame_size = self.channels * self.sample_width_bytes
        return raw_size - (raw_size % frame_size)


class PushToTalkRecorder:
    """Coordinate capture, local transcription, cancellation, and cleanup."""

    def __init__(
        self,
        source: PCMInputSource,
        stt: STTProvider,
        *,
        config: PushToTalkConfig | None = None,
        watchdog_wait: Callable[[float], Awaitable[None]] | None = None,
        temp_registry: TempAssetRegistry | None = None,
    ) -> None:
        self._source = source
        self._stt = stt
        self._config = config or PushToTalkConfig()
        self._watchdog_wait = watchdog_wait or asyncio.sleep
        self._temp_registry = temp_registry
        self._state = RecordingState.idle
        self._pcm = bytearray()
        self._recording_id: str | None = None
        self._recording_limit: asyncio.Event | None = None
        self._recording_watchdog: asyncio.Task[None] | None = None
        self._transcription_task: asyncio.Task[TranscriptionResult] | None = None
        self._operation_task: asyncio.Task[Any] | None = None
        self._send_tasks: set[asyncio.Task[TurnState]] = set()
        self._cancel_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._limit_error: VoiceInputError | None = None
        self._cancel_requested = False
        self._closing = False

    @property
    def state(self) -> RecordingState:
        return self._state

    @property
    def buffered_bytes(self) -> int:
        return len(self._pcm)

    async def start(self) -> None:
        await self._join_cancel_if_running()
        owner = asyncio.current_task()
        assert owner is not None
        async with self._lock:
            if self._closing or self._state is not RecordingState.idle:
                raise VoiceInputError(VoiceInputErrorCode.invalid_state, "当前不能开始录音")
            self._state = RecordingState.recording
            self._pcm.clear()
            self._limit_error = None
            self._cancel_requested = False
            self._recording_id = uuid4().hex
            recording_limit = asyncio.Event()
            self._recording_limit = recording_limit
            recording_id = self._recording_id
            self._loop = asyncio.get_running_loop()
            self._operation_task = owner

        def accept_from_audio_thread(frame: bytes) -> None:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._append_frame, recording_id, frame)

        try:
            await self._source.start(
                accept_from_audio_thread,
                sample_rate=self._config.sample_rate,
                channels=self._config.channels,
            )
            async with self._lock:
                if (
                    self._state is not RecordingState.recording
                    or self._recording_id != recording_id
                ):
                    raise asyncio.CancelledError
                created_watchdog = asyncio.create_task(
                    self._watch_recording_limit(recording_id, recording_limit),
                    name=f"voice-recording-watchdog-{recording_id}",
                )
                self._recording_watchdog = created_watchdog
                created_watchdog.add_done_callback(_consume_task_result)
        except BaseException:
            async with self._lock:
                watchdog_to_stop = self._recording_watchdog
                if self._recording_id == recording_id:
                    self._reset_to_idle()
            await _finish_cleanup(_stop_capture(self._source, watchdog_to_stop))
            raise
        finally:
            async with self._lock:
                if self._operation_task is owner:
                    self._operation_task = None

    def _append_frame(self, recording_id: str, frame: bytes) -> None:
        if self._state is not RecordingState.recording or self._recording_id != recording_id:
            return
        if not frame:
            return
        frame_size = self._config.channels * self._config.sample_width_bytes
        remaining = self._config.max_pcm_bytes - len(self._pcm)
        if remaining <= 0:
            self._limit_error = VoiceInputError(
                VoiceInputErrorCode.recording_too_long, "录音超过最大允许时长"
            )
            if self._recording_limit is not None:
                self._recording_limit.set()
            return
        accepted = min(len(frame), remaining)
        accepted -= accepted % frame_size
        if accepted:
            self._pcm.extend(frame[:accepted])
        if len(frame) > remaining:
            self._limit_error = VoiceInputError(
                VoiceInputErrorCode.recording_too_long, "录音超过最大允许时长"
            )
            if self._recording_limit is not None:
                self._recording_limit.set()

    async def _watch_recording_limit(
        self,
        recording_id: str,
        limit_reached: asyncio.Event,
    ) -> None:
        timer = asyncio.create_task(
            self._wait_for_recording_deadline(),
            name=f"voice-recording-timer-{recording_id}",
        )
        overflow = asyncio.create_task(
            limit_reached.wait(),
            name=f"voice-recording-overflow-{recording_id}",
        )
        try:
            done, pending = await asyncio.wait(
                (timer, overflow),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    # A broken timer must fail closed and stop capture.
                    with suppress(Exception):
                        task.result()
            await self._expire_recording(recording_id)
        finally:
            for task in (timer, overflow):
                if not task.done():
                    task.cancel()
            await asyncio.gather(timer, overflow, return_exceptions=True)

    async def _wait_for_recording_deadline(self) -> None:
        await self._watchdog_wait(self._config.max_recording_seconds)

    async def _expire_recording(self, recording_id: str) -> None:
        async with self._lock:
            if self._state is not RecordingState.recording or self._recording_id != recording_id:
                return
            self._state = RecordingState.timed_out
            self._limit_error = VoiceInputError(
                VoiceInputErrorCode.recording_too_long,
                "录音超过最大允许时长",
            )
            _wipe(self._pcm)
            self._pcm.clear()
            self._recording_id = None
            self._recording_limit = None
        await _finish_cleanup(self._source.stop())

    async def stop(
        self,
        *,
        session_id: str = "local_session",
        user_id: str = "local_user",
    ) -> UserMessage:
        owner = asyncio.current_task()
        assert owner is not None
        async with self._lock:
            timed_out = self._state is RecordingState.timed_out
            if self._state not in {RecordingState.recording, RecordingState.timed_out}:
                raise VoiceInputError(VoiceInputErrorCode.invalid_state, "当前没有正在进行的录音")
            self._state = RecordingState.transcribing
            pcm, self._pcm = self._pcm, bytearray()
            limit_error = self._limit_error
            watchdog = self._recording_watchdog
            self._recording_id = None
            self._recording_limit = None
            self._operation_task = owner
        try:
            if timed_out:
                await _finish_cleanup(_join_cleanup_task(watchdog))
            else:
                await _finish_cleanup(_stop_capture(self._source, watchdog))
            if limit_error is not None:
                raise limit_error
            if not pcm:
                raise VoiceInputError(VoiceInputErrorCode.empty_recording, "录音中没有音频帧")

            temporary_root = self._config.temporary_directory
            if temporary_root is None:
                with tempfile.TemporaryDirectory(prefix="companion-recording-") as root_name:
                    return await self._transcribe_recording(
                        Path(root_name), pcm, session_id=session_id, user_id=user_id
                    )
            root = temporary_root / f"companion-recording-{uuid4().hex}"
            asset_id: str | None = None
            registry = self._temp_registry
            if registry is not None:
                entry = await _drainable_to_thread(
                    lambda: registry.register(
                        root,
                        TempAssetKind.recording_directory,
                    )
                )
                asset_id = entry.asset_id
            root.mkdir(parents=True)
            try:
                return await self._transcribe_recording(
                    root, pcm, session_id=session_id, user_id=user_id
                )
            finally:
                if registry is not None and asset_id is not None:
                    await _drainable_to_thread(
                        lambda: registry.delete(
                            asset_id,
                            ignore_retry_deadline=True,
                        )
                    )
                else:
                    await _drainable_to_thread(lambda: shutil.rmtree(root, ignore_errors=True))
        finally:
            _wipe(pcm)
            async with self._lock:
                self._transcription_task = None
                if self._recording_watchdog is watchdog:
                    self._recording_watchdog = None
                if self._operation_task is owner:
                    self._operation_task = None
                if self._state is not RecordingState.closed:
                    self._reset_to_idle()

    async def _transcribe_recording(
        self,
        root: Path,
        pcm: bytearray,
        *,
        session_id: str,
        user_id: str,
    ) -> UserMessage:
        audio_path = root / "input.wav"
        await _drainable_to_thread(lambda: self._write_wav(audio_path, pcm))
        async with self._lock:
            if self._cancel_requested:
                raise asyncio.CancelledError
            task = asyncio.create_task(
                self._stt.transcribe(
                    TranscriptionRequest(
                        audio_path=audio_path,
                        language=self._config.language,
                        timeout_seconds=self._config.transcription_timeout_seconds,
                    )
                ),
                name="local-stt-transcription",
            )
            self._transcription_task = task
        transcript = await task
        return UserMessage(
            session_id=session_id,
            user_id=user_id,
            text=transcript.text,
            input_mode=InputMode.voice,
            metadata={
                "stt_language": transcript.language,
                "stt_segment_count": transcript.segment_count,
            },
        )

    async def stop_and_send(
        self,
        sink: UserMessageSink,
        *,
        session_id: str = "local_session",
        user_id: str = "local_user",
    ) -> TurnState:
        """Finish local STT and submit the normalized voice message once."""

        message = await self.stop(session_id=session_id, user_id=user_id)
        async with self._lock:
            if self._closing:
                raise asyncio.CancelledError
            task = asyncio.create_task(
                sink.accept(message),
                name=f"voice-message-send-{message.message_id}",
            )
            self._send_tasks.add(task)
            task.add_done_callback(lambda _completed: self._send_tasks.discard(task))
        return await task

    def _write_wav(self, path: Path, pcm: bytearray) -> None:
        with wave.open(str(path), "wb") as recording:
            recording.setnchannels(self._config.channels)
            recording.setsampwidth(self._config.sample_width_bytes)
            recording.setframerate(self._config.sample_rate)
            recording.writeframes(pcm)

    async def cancel(self) -> None:
        async with self._lock:
            task = self._cancel_task
            if task is None or task.done():
                task = asyncio.create_task(self._cancel_impl(), name="voice-input-cancel")
                self._cancel_task = task
        await asyncio.shield(task)

    async def _cancel_impl(self) -> None:
        async with self._lock:
            state = self._state
            transcription = self._transcription_task
            operation = self._operation_task
            watchdog = self._recording_watchdog
            if state is RecordingState.recording:
                self._state = RecordingState.idle
                _wipe(self._pcm)
                self._pcm.clear()
                self._recording_id = None
                self._recording_limit = None
                self._limit_error = None
                self._cancel_requested = True
                if operation is not None and not operation.done():
                    operation.cancel()
            elif state is RecordingState.timed_out:
                self._state = RecordingState.idle
                self._recording_id = None
                self._recording_limit = None
                self._limit_error = None
                self._cancel_requested = True
            elif state is RecordingState.transcribing:
                self._cancel_requested = True
                if transcription is not None and not transcription.done():
                    transcription.cancel()
        if state is RecordingState.recording:
            if operation is not None and operation is not asyncio.current_task():
                await _join_task(operation)
            await _finish_cleanup(_stop_capture(self._source, watchdog))
            async with self._lock:
                if self._recording_watchdog is watchdog:
                    self._recording_watchdog = None
                if self._state is not RecordingState.closed:
                    self._reset_to_idle()
        elif state is RecordingState.timed_out:
            await _finish_cleanup(_join_cleanup_task(watchdog))
            async with self._lock:
                if self._recording_watchdog is watchdog:
                    self._recording_watchdog = None
                if self._state is not RecordingState.closed:
                    self._reset_to_idle()
        elif state is RecordingState.transcribing:
            if operation is not None and operation is not asyncio.current_task():
                await _join_task(operation)
            elif transcription is not None:
                await _join_task(transcription)

    def _reset_to_idle(self) -> None:
        self._state = RecordingState.idle
        _wipe(self._pcm)
        self._pcm.clear()
        self._recording_id = None
        self._recording_limit = None
        self._recording_watchdog = None
        self._limit_error = None
        self._cancel_requested = False

    async def close(self) -> None:
        async with self._lock:
            task = self._close_task
            if task is None:
                self._closing = True
                task = asyncio.create_task(self._close_impl(), name="voice-input-close")
                self._close_task = task
        await asyncio.shield(task)

    async def _close_impl(self) -> None:
        cancellation = await asyncio.gather(self.cancel(), return_exceptions=True)
        async with self._lock:
            sends = tuple(self._send_tasks)
            for task in sends:
                if not task.done():
                    task.cancel()
        send_results = await asyncio.gather(*sends, return_exceptions=True)
        resource_results = await asyncio.gather(
            self._source.close(),
            self._stt.close(),
            return_exceptions=True,
        )
        async with self._lock:
            self._state = RecordingState.closed
            _wipe(self._pcm)
            self._pcm.clear()
            self._recording_id = None
            self._recording_limit = None
            self._recording_watchdog = None
            self._loop = None
        errors = [
            result
            for result in (*cancellation, *send_results, *resource_results)
            if isinstance(result, Exception)
        ]
        if errors:
            raise ExceptionGroup("voice input resource close failed", errors)

    async def _join_cancel_if_running(self) -> None:
        async with self._lock:
            task = self._cancel_task
        if task is not None and not task.done():
            await asyncio.shield(task)


async def _join_task(task: asyncio.Task[Any]) -> None:
    while True:
        try:
            await asyncio.shield(task)
            return
        except asyncio.CancelledError:
            if task.done():
                return
            continue
        except Exception:
            return


async def _join_cleanup_task(task: asyncio.Task[Any] | None) -> None:
    if task is not None:
        await task


async def _stop_capture(
    source: PCMInputSource,
    watchdog: asyncio.Task[None] | None,
) -> None:
    if watchdog is not None and not watchdog.done():
        watchdog.cancel()
    if watchdog is not None:
        await asyncio.gather(watchdog, return_exceptions=True)
    await source.stop()


async def _drainable_to_thread(operation: Callable[[], Any]) -> Any:
    worker = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            cancelled = True
            if worker.done():
                await asyncio.gather(worker, return_exceptions=True)
                raise


async def _finish_cleanup(operation: Awaitable[Any]) -> Any:
    task = asyncio.ensure_future(operation)
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


def _wipe(value: bytearray) -> None:
    if value:
        value[:] = b"\x00" * len(value)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return
