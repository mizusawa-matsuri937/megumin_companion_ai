"""W18 bridge and visible-button PTT tests using transcript-only fakes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

from app.core import TurnService
from app.media.voice import VoiceCaptureError, VoiceCaptureState, VoiceTranscription
from app.schemas import InputMode, TurnState, UserMessage
from desktop_client.inputs.voice_input import PushToTalkRecorder
from desktop_client.ui.backend import (
    BackendContext,
    BackendThreadHost,
    DesktopSessionCursor,
    TurnServiceBackendRuntime,
)
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    BackendState,
    VoiceCancelCommand,
    VoiceInputState,
    VoiceStartCommand,
    VoiceStateEvent,
    VoiceStopCommand,
    VoiceUserMessageEvent,
)
from desktop_client.ui.window import DesktopViewModel, MainWindow
from PySide6.QtWidgets import QApplication


class _VoiceCapture:
    def __init__(self, *, blocks_stop: bool = False) -> None:
        self._state = VoiceCaptureState.idle
        self.blocks_stop = blocks_stop
        self.stop_entered = asyncio.Event()
        self.stop_release = asyncio.Event()
        self.cancelled = False
        self.start_count = 0
        self.cancel_count = 0

    @property
    def state(self) -> VoiceCaptureState:
        return self._state

    async def preflight(self) -> None:
        return None

    async def start(self) -> None:
        if self._state is not VoiceCaptureState.idle:
            raise VoiceCaptureError("voice_invalid_state")
        self.start_count += 1
        self._state = VoiceCaptureState.recording

    async def stop(self) -> VoiceTranscription:
        if self._state is not VoiceCaptureState.recording:
            raise VoiceCaptureError("voice_invalid_state")
        self._state = VoiceCaptureState.transcribing
        self.stop_entered.set()
        if self.blocks_stop:
            await self.stop_release.wait()
        self._state = VoiceCaptureState.idle
        if self.cancelled:
            raise VoiceCaptureError("voice_cancelled")
        return VoiceTranscription("本地按钮转写", "zh", 1)

    async def cancel(self) -> None:
        self.cancel_count += 1
        self.cancelled = True
        self._state = VoiceCaptureState.idle
        self.stop_release.set()

    async def close(self) -> None:
        self._state = VoiceCaptureState.closed


class _VoiceService:
    def __init__(self) -> None:
        self.accepted: list[UserMessage] = []

    async def accept(self, message: UserMessage, *, client_id: str) -> TurnState:
        assert client_id == "desktop_client"
        self.accepted.append(message)
        return TurnState(
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
        )


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("timed out waiting for W18 bridge state")


def test_backend_turns_one_completed_ptt_transcript_into_one_voice_user_message() -> None:
    async def scenario() -> None:
        bridge = ApplicationBridge()
        context = BackendContext(bridge=bridge, generation=1, stop_event=asyncio.Event())
        capture = _VoiceCapture()
        service = _VoiceService()
        runtime = TurnServiceBackendRuntime(
            cast(TurnService, service),
            DesktopSessionCursor(),
            text_chat_available=True,
            voice_input=PushToTalkRecorder(capture),
        )

        await runtime._dispatch_command(context, VoiceStartCommand(command_id="cmd-voice-start"))
        await runtime._dispatch_command(context, VoiceStopCommand(command_id="cmd-voice-stop"))
        await _wait_until(lambda: len(service.accepted) == 1)

        message = service.accepted[0]
        assert message.input_mode is InputMode.voice
        assert message.text == "本地按钮转写"
        assert message.metadata == {"stt_language": "zh", "stt_segment_count": 1}
        events = bridge.drain_events()
        states = [event for event in events if isinstance(event, VoiceStateEvent)]
        assert [event.state for event in states] == [
            VoiceInputState.recording,
            VoiceInputState.transcribing,
            VoiceInputState.idle,
        ]
        voice_message = next(event for event in events if isinstance(event, VoiceUserMessageEvent))
        assert voice_message.message == message
        model = DesktopViewModel()
        model.apply_event(voice_message)
        assert "本地按钮转写" in model.transcript()

    asyncio.run(scenario())


def test_backend_processes_focus_cancel_while_transcription_is_running() -> None:
    async def scenario() -> None:
        bridge = ApplicationBridge()
        context = BackendContext(bridge=bridge, generation=1, stop_event=asyncio.Event())
        capture = _VoiceCapture(blocks_stop=True)
        service = _VoiceService()
        runtime = TurnServiceBackendRuntime(
            cast(TurnService, service),
            DesktopSessionCursor(),
            text_chat_available=True,
            voice_input=PushToTalkRecorder(capture),
        )

        await runtime._dispatch_command(context, VoiceStartCommand(command_id="cmd-focus-start"))
        await runtime._dispatch_command(context, VoiceStopCommand(command_id="cmd-focus-stop"))
        await asyncio.wait_for(capture.stop_entered.wait(), timeout=1)
        await runtime._dispatch_command(
            context,
            VoiceCancelCommand(command_id="cmd-focus-cancel", reason_code="voice_focus_lost"),
        )
        await _wait_until(lambda: runtime._voice_stop_task is None)

        assert capture.cancel_count == 1
        assert service.accepted == []
        states = [event for event in bridge.drain_events() if isinstance(event, VoiceStateEvent)]
        assert any(
            event.state is VoiceInputState.idle and event.reason_code == "voice_focus_lost"
            for event in states
        )

    asyncio.run(scenario())


def test_visible_button_queues_press_release_and_focus_cancel_without_audio_payload(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(bridge, auto_restart_limit=0)
    window = MainWindow(bridge, host)
    window.model.connection_state = BackendState.ready
    window.model.capabilities = BackendCapabilities(
        text_chat=True,
        turn_cancel=True,
        voice_input=True,
    )
    window.model.voice_state = VoiceInputState.idle
    window._sync_view()  # noqa: SLF001 - presentation contract only

    assert window.voice_button.isEnabled()
    assert "按住说话" in window.voice_button.text()
    window._start_voice()  # noqa: SLF001 - direct signal target
    assert isinstance(bridge.take_commands()[0], VoiceStartCommand)
    window._stop_voice()  # noqa: SLF001 - direct signal target
    assert isinstance(bridge.take_commands()[0], VoiceStopCommand)

    window._start_voice()  # noqa: SLF001 - make focus cancellation meaningful
    bridge.take_commands()
    window._cancel_voice("voice_focus_lost")  # noqa: SLF001 - changeEvent target
    command = bridge.take_commands()[0]
    assert isinstance(command, VoiceCancelCommand)
    assert command.reason_code == "voice_focus_lost"
    assert not hasattr(command, "audio")

    window.close()
    qapp.processEvents()
