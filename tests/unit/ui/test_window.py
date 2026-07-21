from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from app.schemas import InputMode, PipelineEvent, TurnState
from desktop_client.ui.backend import BackendContext, BackendThreadHost, SkeletonBackendRuntime
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    BackendState,
    BackendStateEvent,
    BridgeOverflowEvent,
    CommandRejectedEvent,
    TurnCancelCommand,
    UserMessageCommand,
)
from desktop_client.ui.window import DesktopViewModel, MainWindow
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPlainTextEdit


def _pump_until(
    app: QApplication,
    predicate: Callable[[], bool],
    *,
    timeout: float = 3.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.001)
    app.processEvents()
    return predicate()


class _TimelineRuntime:
    async def run(self, context: BackendContext) -> None:
        context.bridge.publish_event(
            BackendStateEvent(
                generation=context.generation,
                state=BackendState.ready,
                capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
            )
        )
        sequence = 0
        while not context.stop_event.is_set():
            commands = await context.next_commands()
            for command in commands:
                if isinstance(command, UserMessageCommand):
                    await asyncio.sleep(0.2)
                    sequence += 1
                    context.bridge.publish_event(
                        PipelineEvent(
                            seq=sequence,
                            type="turn.accepted",
                            turn_id="turn_ui",
                            session_id=command.session_id,
                            payload=TurnState(
                                turn_id="turn_ui",
                                session_id=command.session_id,
                                source_message_id=command.payload.message_id,
                                input_mode=InputMode.text,
                            ).model_dump(mode="json"),
                        )
                    )
                    for text in ("こ", "ん", "に", "ち", "は"):
                        sequence += 1
                        context.bridge.publish_event(
                            PipelineEvent(
                                seq=sequence,
                                type="assistant.delta",
                                turn_id="turn_ui",
                                session_id=command.session_id,
                                payload={"delta": text},
                            )
                        )
                elif isinstance(command, TurnCancelCommand):
                    sequence += 1
                    context.bridge.publish_event(
                        PipelineEvent(
                            seq=sequence,
                            type="turn.cancelled",
                            turn_id=command.payload.turn_id,
                            session_id=command.session_id,
                        )
                    )


def test_window_has_minimum_accessible_controls_and_honest_default_state(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: SkeletonBackendRuntime(),
        auto_restart_limit=0,
    )
    window = MainWindow(bridge, host)
    window.show()
    host.start()
    assert _pump_until(qapp, lambda: window.model.connection_state is BackendState.ready)

    assert isinstance(window.message_view, QPlainTextEdit)
    assert window.message_view.isReadOnly()
    assert window.message_view.accessibleName() == "消息区"
    assert window.editor.accessibleName() == "消息编辑器"
    assert window.send_button.accessibleName() == "发送消息"
    assert window.stop_button.accessibleName() == "停止当前回复"
    assert not window.send_button.isEnabled()
    assert not window.stop_button.isEnabled()
    assert "\u4e0d\u53ef\u7528" in window.feature_status.text()

    window.editor.setPlainText("DRAFT_PRIVATE_SENTINEL")
    window.close()
    assert _pump_until(qapp, lambda: not window.isVisible())
    assert window.editor.toPlainText() == ""
    assert bridge.command_count == 0
    assert bridge.event_count == 0


def test_editor_tab_moves_focus_without_mutating_draft(qapp: QApplication) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: SkeletonBackendRuntime(),
        auto_restart_limit=0,
    )
    window = MainWindow(bridge, host)
    window.show()
    window.activateWindow()
    qapp.processEvents()

    window.message_view.setFocus()
    QTest.keyClick(window.message_view, Qt.Key.Key_Tab)
    assert qapp.focusWidget() is window.editor

    window.editor.setPlainText("DRAFT_PRIVATE_SENTINEL")
    QTest.keyClick(window.editor, Qt.Key.Key_Tab)
    assert qapp.focusWidget() is window.message_view
    assert window.editor.toPlainText() == "DRAFT_PRIVATE_SENTINEL"

    QTest.keyClick(window.message_view, Qt.Key.Key_Backtab)
    assert qapp.focusWidget() is window.editor
    assert window.editor.toPlainText() == "DRAFT_PRIVATE_SENTINEL"
    window.close()


def test_dynamic_status_accessible_names_follow_visible_values(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: SkeletonBackendRuntime(),
        auto_restart_limit=0,
    )
    window = MainWindow(bridge, host)
    window.show()

    assert window.connection_status.accessibleName() == "后端：已停止"
    assert window.feature_status.accessibleName() == "文字聊天：不可用"

    bridge.publish_event(
        BackendStateEvent(
            generation=1,
            state=BackendState.failed,
            reason_code="backend_crashed",
        )
    )
    assert _pump_until(qapp, lambda: "backend_crashed" in window.connection_status.text())
    assert window.connection_status.accessibleName() == window.connection_status.text()

    bridge.publish_event(
        BackendStateEvent(
            generation=2,
            state=BackendState.ready,
            capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
        )
    )
    assert _pump_until(qapp, lambda: window.feature_status.text() == "文字聊天：可用")
    assert window.feature_status.accessibleName() == window.feature_status.text()
    window.close()


def test_window_fake_timeline_keeps_qt_responsive_and_handles_cancel(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: _TimelineRuntime(),
        auto_restart_limit=0,
    )
    window = MainWindow(bridge, host)
    window.show()
    host.start()
    assert _pump_until(qapp, lambda: window.send_button.isEnabled())
    message_view = window.message_view
    assert isinstance(message_view, QPlainTextEdit)

    window.editor.setPlainText("中文输入テスト")
    timer_fired: list[bool] = []
    QTimer.singleShot(0, lambda: timer_fired.append(True))
    click_started = time.monotonic()
    window.send_button.click()
    assert time.monotonic() - click_started < 0.1
    assert _pump_until(qapp, lambda: bool(timer_fired), timeout=0.1)
    assert window.editor.toPlainText() == ""
    assert _pump_until(qapp, lambda: "こんにちは" in message_view.toPlainText())
    assert "你: 中文输入テスト" in message_view.toPlainText()
    assert window.stop_button.isEnabled()

    window.stop_button.click()
    assert _pump_until(qapp, lambda: window.model.active_turn_id is None)
    assert not window.stop_button.isEnabled()
    window.close()
    assert _pump_until(qapp, lambda: not window.isVisible())


def test_view_model_handles_empty_error_loading_and_bounded_messages() -> None:
    model = DesktopViewModel()
    assert model.transcript() == ""
    model.apply_event(BackendStateEvent(generation=1, state=BackendState.starting))
    assert model.connection_state is BackendState.starting
    model.apply_event(CommandRejectedEvent(command_id="cmd_1", reason_code="busy"))
    assert model.last_error_code == "busy"
    overflow_model = DesktopViewModel()
    overflow_model.apply_event(BridgeOverflowEvent(dropped_count=512))
    assert overflow_model.connection_state is BackendState.degraded
    assert overflow_model.last_error_code == "event_snapshot_required"

    for index in range(205):
        model.apply_event(
            PipelineEvent(
                seq=index + 1,
                type="assistant.delta",
                turn_id=f"turn_{index}",
                session_id="local_session",
                payload={"delta": "x"},
            )
        )
    assert model.transcript().count("助手:") == 200
    model.apply_event(
        PipelineEvent(
            seq=206,
            type="turn.accepted",
            turn_id="turn_active",
            session_id="local_session",
            payload=TurnState(
                turn_id="turn_active",
                session_id="local_session",
                source_message_id="message_active",
                input_mode=InputMode.text,
            ).model_dump(mode="json"),
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=207,
            type="turn.failed",
            turn_id="turn_active",
            session_id="local_session",
            payload={"error_code": "provider_failed"},
        )
    )
    assert model.active_turn_id is None
    assert model.last_error_code == "provider_failed"
    model.apply_event(
        PipelineEvent(
            seq=208,
            type="turn.accepted",
            turn_id="turn_private",
            session_id="local_session",
            payload=TurnState(
                turn_id="turn_private",
                session_id="local_session",
                source_message_id="message_private",
                input_mode=InputMode.text,
            ).model_dump(mode="json"),
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=209,
            type="turn.failed",
            turn_id="turn_private",
            session_id="local_session",
            payload={"error_code": "PRIVATE exception body"},
        )
    )
    assert model.last_error_code == "turn_failed"
    model.clear_sensitive()
    assert model.transcript() == ""


def test_view_model_applies_accepted_delta_segment_and_terminal_in_sequence() -> None:
    model = DesktopViewModel()
    state = TurnState(
        turn_id="turn-sequenced",
        session_id="local_session",
        source_message_id="message-sequenced",
        input_mode=InputMode.text,
    )
    model.apply_event(
        PipelineEvent(
            seq=1,
            type="turn.accepted",
            turn_id=state.turn_id,
            session_id=state.session_id,
            payload=state.model_dump(mode="json"),
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=2,
            type="assistant.delta",
            turn_id=state.turn_id,
            session_id=state.session_id,
            payload={"delta": "streamed reply"},
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=3,
            type="assistant.segment",
            turn_id=state.turn_id,
            session_id=state.session_id,
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=4,
            type="assistant.completed",
            turn_id=state.turn_id,
            session_id=state.session_id,
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=4,
            type="assistant.delta",
            turn_id=state.turn_id,
            session_id=state.session_id,
            payload={"delta": "must-be-ignored"},
        )
    )

    assert model.active_turn_id is None
    assert model.segment_count == 1
    assert "streamed reply" in model.transcript()
    assert "must-be-ignored" not in model.transcript()
