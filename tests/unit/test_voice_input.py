"""Push-to-talk state, PCM format, cancellation, and cleanup tests."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from app.schemas import InputMode, TurnState, UserMessage
from desktop_client.inputs.stt_contracts import TranscriptionRequest, TranscriptionResult
from desktop_client.inputs.voice_input import (
    PCMCallback,
    PushToTalkConfig,
    PushToTalkRecorder,
    RecordingState,
    SoundDevicePCMInput,
    VoiceInputError,
    VoiceInputErrorCode,
)


class FakePCMSource:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.callback: PCMCallback | None = None
        self.fail_start = fail_start
        self.start_count = 0
        self.stop_count = 0
        self.close_count = 0

    async def start(self, callback: PCMCallback, *, sample_rate: int, channels: int) -> None:
        assert sample_rate == 16_000
        assert channels == 1
        self.start_count += 1
        if self.fail_start:
            raise RuntimeError("permission denied")
        self.callback = callback

    def emit(self, frame: bytes) -> None:
        assert self.callback is not None
        self.callback(frame)

    async def stop(self) -> None:
        self.stop_count += 1
        self.callback = None

    async def close(self) -> None:
        self.close_count += 1
        await self.stop()


class InspectingSTT:
    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.formats: list[tuple[int, int, int, int]] = []
        self.closed = False

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.paths.append(request.audio_path)
        with wave.open(str(request.audio_path), "rb") as recording:
            self.formats.append(
                (
                    recording.getframerate(),
                    recording.getnchannels(),
                    recording.getsampwidth(),
                    recording.getnframes(),
                )
            )
        return TranscriptionResult(text="转写完成", language="zh", segment_count=1)

    async def close(self) -> None:
        self.closed = True


class BlockingSTT:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.path: Path | None = None
        self.cancelled = False

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.path = request.audio_path
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None


class RecordingMessageSink:
    def __init__(self) -> None:
        self.messages: list[UserMessage] = []

    async def accept(self, message: UserMessage) -> TurnState:
        self.messages.append(message)
        return TurnState(
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
        )


class SlowStoppingSource(FakePCMSource):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.stop_started = asyncio.Event()
        self.release_stop = asyncio.Event()
        self.fail = fail

    async def stop(self) -> None:
        self.stop_started.set()
        await self.release_stop.wait()
        self.stop_count += 1
        self.callback = None
        if self.fail:
            raise RuntimeError("device stop failed")


class FakeRawInputStream:
    def __init__(self, kwargs: dict[str, Any], *, fail_start: bool = False) -> None:
        self.kwargs = kwargs
        self.fail_start = fail_start
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("microphone unavailable")
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakeSoundDevice:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.streams: list[FakeRawInputStream] = []

    def RawInputStream(self, **kwargs: Any) -> FakeRawInputStream:  # noqa: N802
        stream = FakeRawInputStream(kwargs, fail_start=self.fail_start)
        self.streams.append(stream)
        return stream


def _assert_state(recorder: PushToTalkRecorder, expected: RecordingState) -> None:
    assert recorder.state is expected


def test_audio_device_stays_closed_until_explicit_start_and_wav_is_temporary(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = FakePCMSource()
        stt = InspectingSTT()
        scratch = tmp_path / "recordings"
        recorder = PushToTalkRecorder(
            source,
            stt,
            config=PushToTalkConfig(temporary_directory=scratch),
        )
        assert source.start_count == 0
        _assert_state(recorder, RecordingState.idle)

        await recorder.start()
        source.emit(b"\x01\x00" * 320)
        await asyncio.sleep(0)
        result = await recorder.stop()

        assert result.text == "转写完成"
        assert result.input_mode is InputMode.voice
        assert result.metadata == {"stt_language": "zh", "stt_segment_count": 1}
        assert stt.formats == [(16_000, 1, 2, 320)]
        assert stt.paths and not stt.paths[0].exists()
        assert list(scratch.iterdir()) == []
        assert recorder.buffered_bytes == 0
        _assert_state(recorder, RecordingState.idle)
        await recorder.close()
        assert stt.closed
        _assert_state(recorder, RecordingState.closed)

    asyncio.run(scenario())


def test_cancel_during_recording_discards_pcm_without_invoking_stt() -> None:
    async def scenario() -> None:
        source = FakePCMSource()
        stt = InspectingSTT()
        recorder = PushToTalkRecorder(source, stt)
        await recorder.start()
        source.emit(b"\x00\x00" * 10)
        await asyncio.sleep(0)
        await recorder.cancel()
        assert recorder.state is RecordingState.idle
        assert recorder.buffered_bytes == 0
        assert stt.paths == []
        assert source.stop_count == 1
        await recorder.close()

    asyncio.run(scenario())


def test_stop_and_send_delivers_one_normalized_voice_message() -> None:
    async def scenario() -> None:
        source = FakePCMSource()
        recorder = PushToTalkRecorder(source, InspectingSTT())
        sink = RecordingMessageSink()
        await recorder.start()
        source.emit(b"\x00\x00" * 16)
        await asyncio.sleep(0)
        state = await recorder.stop_and_send(sink, session_id="voice-session", user_id="user-1")
        assert len(sink.messages) == 1
        message = sink.messages[0]
        assert message.session_id == "voice-session"
        assert message.user_id == "user-1"
        assert message.input_mode is InputMode.voice
        assert state.source_message_id == message.message_id
        _assert_state(recorder, RecordingState.idle)
        await recorder.close()

    asyncio.run(scenario())


def test_cancel_during_transcription_cancels_provider_and_deletes_wav(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = FakePCMSource()
        stt = BlockingSTT()
        scratch = tmp_path / "recordings"
        recorder = PushToTalkRecorder(
            source,
            stt,
            config=PushToTalkConfig(temporary_directory=scratch),
        )
        await recorder.start()
        source.emit(b"\x00\x00" * 10)
        await asyncio.sleep(0)
        stop_task = asyncio.create_task(recorder.stop())
        await stt.started.wait()
        assert stt.path is not None and stt.path.exists()

        await recorder.cancel()
        results = await asyncio.gather(stop_task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert stt.cancelled
        assert stt.path is not None and not stt.path.exists()
        assert list(scratch.iterdir()) == []
        assert recorder.state is RecordingState.idle
        await recorder.close()

    asyncio.run(scenario())


def test_empty_and_over_limit_recordings_fail_without_stt() -> None:
    async def scenario() -> None:
        source = FakePCMSource()
        stt = InspectingSTT()
        recorder = PushToTalkRecorder(source, stt)
        await recorder.start()
        with pytest.raises(VoiceInputError) as empty:
            await recorder.stop()
        assert empty.value.code is VoiceInputErrorCode.empty_recording

        tiny_limit = 4 / (16_000 * 2)
        recorder = PushToTalkRecorder(
            source,
            stt,
            config=PushToTalkConfig(max_recording_seconds=tiny_limit),
        )
        await recorder.start()
        source.emit(b"\x00" * 6)
        await asyncio.sleep(0)
        assert recorder.buffered_bytes == 4
        with pytest.raises(VoiceInputError) as too_long:
            await recorder.stop()
        assert too_long.value.code is VoiceInputErrorCode.recording_too_long
        assert stt.paths == []
        await recorder.close()

    asyncio.run(scenario())


def test_invalid_transitions_and_start_failure_return_to_idle() -> None:
    async def scenario() -> None:
        source = FakePCMSource(fail_start=True)
        recorder = PushToTalkRecorder(source, InspectingSTT())
        with pytest.raises(RuntimeError, match="permission denied"):
            await recorder.start()
        assert recorder.state is RecordingState.idle
        with pytest.raises(VoiceInputError) as caught:
            await recorder.stop()
        assert caught.value.code is VoiceInputErrorCode.invalid_state
        await recorder.close()
        with pytest.raises(VoiceInputError):
            await recorder.start()

    asyncio.run(scenario())


def test_sounddevice_adapter_is_lazy_filters_status_and_releases_stream() -> None:
    async def scenario() -> None:
        module = FakeSoundDevice()
        received: list[bytes] = []
        source = SoundDevicePCMInput(device="test-mic", blocksize=64)
        await source.stop()
        with patch(
            "desktop_client.inputs.voice_input.importlib.import_module", return_value=module
        ):
            await source.start(received.append, sample_rate=16_000, channels=1)
        stream = module.streams[0]
        callback = stream.kwargs["callback"]
        callback(memoryview(b"\x01\x00"), 1, None, False)
        callback(memoryview(b"\x02\x00"), 1, None, True)
        assert received == [b"\x01\x00"]
        assert stream.kwargs["dtype"] == "int16"
        assert stream.kwargs["device"] == "test-mic"
        with pytest.raises(VoiceInputError) as duplicate:
            await source.start(received.append, sample_rate=16_000, channels=1)
        assert duplicate.value.code is VoiceInputErrorCode.invalid_state
        await source.stop()
        assert stream.started and stream.stopped and stream.closed
        await source.close()

    asyncio.run(scenario())


def test_sounddevice_start_failures_are_typed_and_partial_stream_is_closed() -> None:
    async def scenario() -> None:
        module = FakeSoundDevice(fail_start=True)
        source = SoundDevicePCMInput()
        with (
            patch("desktop_client.inputs.voice_input.importlib.import_module", return_value=module),
            pytest.raises(VoiceInputError) as caught,
        ):
            await source.start(lambda _frame: None, sample_rate=16_000, channels=1)
        assert caught.value.code is VoiceInputErrorCode.capture_failed
        assert module.streams[0].closed

        with (
            patch(
                "desktop_client.inputs.voice_input.importlib.import_module",
                side_effect=ImportError("missing"),
            ),
            pytest.raises(VoiceInputError) as missing,
        ):
            await source.start(lambda _frame: None, sample_rate=16_000, channels=1)
        assert missing.value.code is VoiceInputErrorCode.capture_failed

    asyncio.run(scenario())


def test_configuration_validation_duplicate_start_and_second_overflow_frame() -> None:
    with pytest.raises(ValueError, match="固定"):
        PushToTalkConfig(sample_rate=8_000)
    with pytest.raises(ValueError, match="超时"):
        PushToTalkConfig(max_recording_seconds=0)

    async def scenario() -> None:
        source = FakePCMSource()
        recorder = PushToTalkRecorder(
            source,
            InspectingSTT(),
            config=PushToTalkConfig(max_recording_seconds=4 / (16_000 * 2)),
        )
        await recorder.start()
        with pytest.raises(VoiceInputError) as duplicate:
            await recorder.start()
        assert duplicate.value.code is VoiceInputErrorCode.invalid_state
        source.emit(b"")
        source.emit(b"\x00" * 4)
        source.emit(b"\x00" * 2)
        await asyncio.sleep(0)
        with pytest.raises(VoiceInputError) as overflow:
            await recorder.stop()
        assert overflow.value.code is VoiceInputErrorCode.recording_too_long
        await recorder.close()
        await recorder.close()

    asyncio.run(scenario())


def test_cancel_between_capture_stop_and_transcription_prevents_stt() -> None:
    async def scenario() -> None:
        source = SlowStoppingSource()
        stt = InspectingSTT()
        recorder = PushToTalkRecorder(source, stt)
        await recorder.start()
        source.emit(b"\x00\x00" * 4)
        await asyncio.sleep(0)
        stop_task = asyncio.create_task(recorder.stop())
        await source.stop_started.wait()
        await recorder.cancel()
        source.release_stop.set()
        result = await asyncio.gather(stop_task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert stt.paths == []
        _assert_state(recorder, RecordingState.idle)
        await recorder.close()

    asyncio.run(scenario())


def test_capture_stop_failure_resets_recorder_state() -> None:
    async def scenario() -> None:
        source = SlowStoppingSource(fail=True)
        recorder = PushToTalkRecorder(source, InspectingSTT())
        await recorder.start()
        source.emit(b"\x00\x00" * 4)
        await asyncio.sleep(0)
        task = asyncio.create_task(recorder.stop())
        await source.stop_started.wait()
        source.release_stop.set()
        with pytest.raises(RuntimeError, match="device stop failed"):
            await task
        _assert_state(recorder, RecordingState.idle)
        source.fail = False
        await recorder.close()

    asyncio.run(scenario())
