"""Desktop-side PTT state adapter with no microphone or PCM ownership.

Native capture, the bounded PCM ring, temporary WAV/JSON, and whisper-cli all
live inside ``MediaWorker``.  This module only turns a completed bounded
transcript into the existing voice ``UserMessage`` contract.
"""

from __future__ import annotations

from typing import Protocol

from app.core.contracts import UserMessageSink
from app.media.voice import (
    MediaWorkerVoiceInput,
    VoiceCaptureError,
    VoiceCaptureState,
    VoiceTranscription,
)
from app.schemas import InputMode, TurnState, UserMessage


class VoiceCapture(Protocol):
    @property
    def state(self) -> VoiceCaptureState: ...

    async def preflight(self) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> VoiceTranscription: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


RecordingState = VoiceCaptureState
VoiceInputError = VoiceCaptureError


class PushToTalkRecorder:
    """Submit one explicit local voice message after MediaWorker transcription."""

    def __init__(self, capture: VoiceCapture) -> None:
        self._capture = capture

    @property
    def state(self) -> RecordingState:
        return self._capture.state

    async def start(self) -> None:
        await self._capture.start()

    async def preflight(self) -> None:
        """Check the configured local helper without acquiring a microphone."""

        await self._capture.preflight()

    async def stop(
        self,
        *,
        session_id: str = "local_session",
        user_id: str = "local_user",
    ) -> UserMessage:
        transcript = await self._capture.stop()
        return _voice_message(transcript, session_id=session_id, user_id=user_id)

    async def stop_and_send(
        self,
        sink: UserMessageSink,
        *,
        session_id: str = "local_session",
        user_id: str = "local_user",
    ) -> TurnState:
        return await sink.accept(await self.stop(session_id=session_id, user_id=user_id))

    async def cancel(self) -> None:
        await self._capture.cancel()

    async def close(self) -> None:
        await self._capture.close()


def _voice_message(
    transcript: VoiceTranscription,
    *,
    session_id: str,
    user_id: str,
) -> UserMessage:
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


__all__ = [
    "MediaWorkerVoiceInput",
    "PushToTalkRecorder",
    "RecordingState",
    "VoiceCapture",
    "VoiceInputError",
]
