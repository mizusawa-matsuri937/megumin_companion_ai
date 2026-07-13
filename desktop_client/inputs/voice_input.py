"""Explicit push-to-talk recording state machine.

No audio device is opened until :meth:`PushToTalkRecorder.start` is called.
Raw PCM remains in memory while recording and in a private temporary WAV only
while the local STT provider is running.
"""

from __future__ import annotations

import asyncio
import importlib
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.schemas import InputMode, UserMessage
from desktop_client.inputs.stt_contracts import (
    STTProvider,
    TranscriptionRequest,
    TranscriptionResult,
)

PCMCallback = Callable[[bytes], None]


class RecordingState(StrEnum):
    idle = "idle"
    recording = "recording"
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
            await asyncio.to_thread(stream.start)
        except Exception as exc:
            if "stream" in locals():
                await asyncio.to_thread(stream.close)
            raise VoiceInputError(
                VoiceInputErrorCode.capture_failed, "无法启动本地麦克风录音"
            ) from exc
        self._stream = stream

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            await asyncio.to_thread(stream.stop)
        finally:
            await asyncio.to_thread(stream.close)

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
    ) -> None:
        self._source = source
        self._stt = stt
        self._config = config or PushToTalkConfig()
        self._state = RecordingState.idle
        self._pcm = bytearray()
        self._recording_id: str | None = None
        self._transcription_task: asyncio.Task[TranscriptionResult] | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._limit_error: VoiceInputError | None = None
        self._cancel_requested = False

    @property
    def state(self) -> RecordingState:
        return self._state

    @property
    def buffered_bytes(self) -> int:
        return len(self._pcm)

    async def start(self) -> None:
        async with self._lock:
            if self._state is not RecordingState.idle:
                raise VoiceInputError(VoiceInputErrorCode.invalid_state, "当前不能开始录音")
            self._state = RecordingState.recording
            self._pcm.clear()
            self._limit_error = None
            self._cancel_requested = False
            self._recording_id = uuid4().hex
            recording_id = self._recording_id
            self._loop = asyncio.get_running_loop()

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
        except BaseException:
            async with self._lock:
                if self._recording_id == recording_id:
                    self._reset_to_idle()
            raise

    def _append_frame(self, recording_id: str, frame: bytes) -> None:
        if self._state is not RecordingState.recording or self._recording_id != recording_id:
            return
        if not frame:
            return
        remaining = self._config.max_pcm_bytes - len(self._pcm)
        if remaining <= 0:
            self._limit_error = VoiceInputError(
                VoiceInputErrorCode.recording_too_long, "录音超过最大允许时长"
            )
            return
        self._pcm.extend(frame[:remaining])
        if len(frame) > remaining:
            self._limit_error = VoiceInputError(
                VoiceInputErrorCode.recording_too_long, "录音超过最大允许时长"
            )

    async def stop(
        self,
        *,
        session_id: str = "local_session",
        user_id: str = "local_user",
    ) -> UserMessage:
        async with self._lock:
            if self._state is not RecordingState.recording:
                raise VoiceInputError(VoiceInputErrorCode.invalid_state, "当前没有正在进行的录音")
            self._state = RecordingState.transcribing
            pcm = bytes(self._pcm)
            self._pcm.clear()
            limit_error = self._limit_error
            self._recording_id = None
        try:
            await self._source.stop()
            if limit_error is not None:
                raise limit_error
            if not pcm:
                raise VoiceInputError(VoiceInputErrorCode.empty_recording, "录音中没有音频帧")

            temporary_root = self._config.temporary_directory
            if temporary_root is not None:
                temporary_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="companion-recording-", dir=temporary_root
            ) as root:
                audio_path = Path(root) / "input.wav"
                self._write_wav(audio_path, pcm)
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
        finally:
            async with self._lock:
                self._transcription_task = None
                if self._state is not RecordingState.closed:
                    self._reset_to_idle()

    def _write_wav(self, path: Path, pcm: bytes) -> None:
        with wave.open(str(path), "wb") as recording:
            recording.setnchannels(self._config.channels)
            recording.setsampwidth(self._config.sample_width_bytes)
            recording.setframerate(self._config.sample_rate)
            recording.writeframes(pcm)

    async def cancel(self) -> None:
        async with self._lock:
            state = self._state
            task = self._transcription_task
            if state is RecordingState.recording:
                self._reset_to_idle()
            elif state is RecordingState.transcribing:
                self._cancel_requested = True
                if task is not None:
                    task.cancel()
        if state is RecordingState.recording:
            await self._source.stop()
        elif state is RecordingState.transcribing and task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def _reset_to_idle(self) -> None:
        self._state = RecordingState.idle
        self._pcm.clear()
        self._recording_id = None
        self._limit_error = None
        self._cancel_requested = False

    async def close(self) -> None:
        async with self._lock:
            if self._state is RecordingState.closed:
                return
        await self.cancel()
        await self._source.close()
        await self._stt.close()
        async with self._lock:
            self._state = RecordingState.closed
            self._pcm.clear()
            self._recording_id = None
            self._loop = None
