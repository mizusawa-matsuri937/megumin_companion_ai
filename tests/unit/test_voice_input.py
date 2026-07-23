"""Desktop-side PTT adapter tests: no PCM or microphone belongs here."""

from __future__ import annotations

import asyncio

from app.media.voice import VoiceCaptureState, VoiceTranscription
from app.schemas import InputMode, TurnState, TurnStatus, UserMessage
from desktop_client.inputs.voice_input import PushToTalkRecorder


class _FakeCapture:
    def __init__(self) -> None:
        self._state = VoiceCaptureState.idle
        self.start_count = 0
        self.cancel_count = 0
        self.closed = False
        self.transcript = VoiceTranscription("  本地转写完成  ", "zh", 2)

    @property
    def state(self) -> VoiceCaptureState:
        return self._state

    async def preflight(self) -> None:
        assert self._state is VoiceCaptureState.idle

    async def start(self) -> None:
        assert self._state is VoiceCaptureState.idle
        self.start_count += 1
        self._state = VoiceCaptureState.recording

    async def stop(self) -> VoiceTranscription:
        assert self._state is VoiceCaptureState.recording
        self._state = VoiceCaptureState.idle
        return self.transcript

    async def cancel(self) -> None:
        self.cancel_count += 1
        if self._state is not VoiceCaptureState.closed:
            self._state = VoiceCaptureState.idle

    async def close(self) -> None:
        self.closed = True
        self._state = VoiceCaptureState.closed


class _Sink:
    def __init__(self) -> None:
        self.messages: list[UserMessage] = []

    async def accept(self, message: UserMessage) -> TurnState:
        self.messages.append(message)
        return TurnState(
            turn_id="turn_voice",
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
            status=TurnStatus.accepted,
        )


def test_stop_builds_one_voice_user_message_without_audio_body() -> None:
    async def scenario() -> None:
        capture = _FakeCapture()
        recorder = PushToTalkRecorder(capture)

        await recorder.start()
        message = await recorder.stop(session_id="session_1", user_id="user_1")

        assert recorder.state is VoiceCaptureState.idle
        assert message.session_id == "session_1"
        assert message.user_id == "user_1"
        assert message.text == "本地转写完成"
        assert message.input_mode is InputMode.voice
        assert message.metadata == {"stt_language": "zh", "stt_segment_count": 2}
        assert not hasattr(recorder, "buffered_bytes")

    asyncio.run(scenario())


def test_stop_and_send_delegates_one_normalized_voice_message() -> None:
    async def scenario() -> None:
        capture = _FakeCapture()
        recorder = PushToTalkRecorder(capture)
        sink = _Sink()

        await recorder.start()
        state = await recorder.stop_and_send(sink)

        assert state.status == "accepted"
        assert len(sink.messages) == 1
        assert sink.messages[0].input_mode is InputMode.voice

    asyncio.run(scenario())


def test_cancel_and_close_delegate_to_the_media_worker_owner() -> None:
    async def scenario() -> None:
        capture = _FakeCapture()
        recorder = PushToTalkRecorder(capture)

        await recorder.start()
        await recorder.cancel()
        assert capture.cancel_count == 1
        assert recorder.state is VoiceCaptureState.idle
        await recorder.close()
        assert capture.closed
        assert recorder.state.value == "closed"

    asyncio.run(scenario())
