"""Accessible Qt Widgets shell for W14 text chat and recovery."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas import (
    PipelineEvent,
    SessionReset,
    SessionSnapshotChunk,
    TurnInterruptRequest,
    TurnState,
    TurnStatus,
    UserMessage,
)
from pydantic import ValidationError
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from desktop_client.ui.backend import BackendThreadHost
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    BackendState,
    BackendStateEvent,
    BridgeEvent,
    BridgeOverflowEvent,
    CommandRejectedEvent,
    TurnCancelCommand,
    UserMessageCommand,
    is_stable_reason_code,
)

MAX_VISIBLE_MESSAGES = 200
MAX_VISIBLE_MESSAGE_CHARS = 100_000


@dataclass(slots=True)
class _VisibleMessage:
    role: str
    text: str
    turn_id: str | None = None


class DesktopViewModel:
    """In-memory-only W14 presentation state; no repository or persistence access."""

    def __init__(self) -> None:
        self.connection_state = BackendState.stopped
        self.capabilities = BackendCapabilities()
        self.active_turn_id: str | None = None
        self.last_error_code: str | None = None
        self.last_seq = 0
        self.segment_count = 0
        self.snapshot_pending = False
        self._snapshot_request_pending = False
        self._snapshot_id: str | None = None
        self._snapshot_chunk_count = 0
        self._next_snapshot_chunk = 0
        self._turn_states: dict[str, TurnStatus] = {}
        self._shown_user_message_ids: set[str] = set()
        self._messages: list[_VisibleMessage] = []

    def add_user_message(self, message: UserMessage) -> None:
        if message.message_id in self._shown_user_message_ids:
            return
        self._shown_user_message_ids.add(message.message_id)
        self._append(_VisibleMessage(role="你", text=message.text))

    def apply_event(self, event: BridgeEvent) -> None:
        if isinstance(event, SessionReset):
            self._apply_reset(event)
            return
        if isinstance(event, SessionSnapshotChunk):
            self._apply_snapshot_chunk(event)
            return
        if isinstance(event, BackendStateEvent):
            self.connection_state = event.state
            self.capabilities = event.capabilities
            self.last_error_code = event.reason_code
            return
        if isinstance(event, CommandRejectedEvent):
            self.last_error_code = event.reason_code
            return
        if isinstance(event, BridgeOverflowEvent):
            self.connection_state = BackendState.degraded
            self._require_snapshot()
            return
        self._apply_pipeline_event(event)

    def _apply_pipeline_event(self, event: PipelineEvent) -> None:
        if not self._accept_sequence(event):
            return
        if event.type in {"turn.accepted", "turn.snapshot"}:
            self._apply_turn_payload(event)
            return
        if event.type == "assistant.delta":
            delta = event.payload.get("delta")
            if isinstance(delta, str) and delta:
                if (
                    self._messages
                    and self._messages[-1].role == "助手"
                    and self._messages[-1].turn_id == event.turn_id
                ):
                    message = self._messages[-1]
                    message.text = (message.text + delta)[-MAX_VISIBLE_MESSAGE_CHARS:]
                else:
                    self._append(
                        _VisibleMessage(
                            role="助手",
                            text=delta[-MAX_VISIBLE_MESSAGE_CHARS:],
                            turn_id=event.turn_id,
                        )
                    )
            return
        if event.type == "assistant.segment":
            self.segment_count += 1
            return
        if event.type in {"assistant.output_incomplete", "audio.degraded", "playback.skipped"}:
            self._apply_error_payload(event)
            return
        if event.type == "assistant.truncated":
            self._apply_error_payload(event)
            return
        if event.type == "assistant.completed":
            if event.turn_id == self.active_turn_id:
                self.active_turn_id = None
            return
        if event.type in {"turn.completed", "turn.cancelled", "turn.failed"}:
            if event.turn_id == self.active_turn_id:
                self.active_turn_id = None
            if event.type == "turn.failed":
                error_code = event.payload.get("error_code")
                self.last_error_code = (
                    error_code
                    if isinstance(error_code, str) and is_stable_reason_code(error_code)
                    else "turn_failed"
                )

    def _accept_sequence(self, event: PipelineEvent) -> bool:
        if self.snapshot_pending:
            return False
        if event.seq <= self.last_seq:
            return False
        start_seq = event.payload.get("coalesced_from_seq", event.seq)
        if (
            not isinstance(start_seq, int)
            or start_seq != self.last_seq + 1
            or event.seq < start_seq
        ):
            self._require_snapshot()
            return False
        self.last_seq = event.seq
        return True

    def _apply_turn_payload(self, event: PipelineEvent) -> None:
        try:
            state = TurnState.model_validate(event.payload)
        except ValidationError:
            self.last_error_code = "invalid_turn_state"
            return
        self._turn_states[state.turn_id] = state.status
        if state.status in {TurnStatus.accepted, TurnStatus.streaming, TurnStatus.speaking}:
            self.active_turn_id = state.turn_id
        elif state.turn_id == self.active_turn_id:
            self.active_turn_id = None
        if state.status is TurnStatus.failed:
            self.last_error_code = (
                state.error_code
                if state.error_code is not None and is_stable_reason_code(state.error_code)
                else "turn_failed"
            )

    def _apply_error_payload(self, event: PipelineEvent) -> None:
        error_code = event.payload.get("error_code", event.payload.get("reason"))
        if isinstance(error_code, str) and is_stable_reason_code(error_code):
            self.last_error_code = error_code

    def _apply_reset(self, event: SessionReset) -> None:
        if event.snapshot.last_seq != event.reset_to_seq:
            self._require_snapshot()
            return
        self._clear_visible_bodies()
        self.last_seq = event.reset_to_seq
        self.snapshot_pending = event.snapshot.chunk_count > 0
        self._snapshot_id = event.reset_id if self.snapshot_pending else None
        self._snapshot_chunk_count = event.snapshot.chunk_count
        self._next_snapshot_chunk = 0
        self.active_turn_id = None
        self._turn_states.clear()

    def _apply_snapshot_chunk(self, event: SessionSnapshotChunk) -> None:
        if (
            not self.snapshot_pending
            or event.reset_id != self._snapshot_id
            or event.chunk_count != self._snapshot_chunk_count
            or event.chunk_index != self._next_snapshot_chunk
        ):
            self._require_snapshot()
            return
        for state in event.turns:
            self._turn_states[state.turn_id] = state.status
            if state.status in {TurnStatus.accepted, TurnStatus.streaming, TurnStatus.speaking}:
                self.active_turn_id = state.turn_id
            if (
                state.status is TurnStatus.failed
                and state.error_code is not None
                and is_stable_reason_code(state.error_code)
            ):
                self.last_error_code = state.error_code
        self._next_snapshot_chunk += 1
        if self._next_snapshot_chunk == self._snapshot_chunk_count:
            self.snapshot_pending = False
            self._snapshot_id = None

    def _require_snapshot(self) -> None:
        self._clear_visible_bodies()
        self.active_turn_id = None
        self.snapshot_pending = True
        self._snapshot_id = None
        self._snapshot_chunk_count = 0
        self._next_snapshot_chunk = 0
        self.last_error_code = "event_snapshot_required"
        self._snapshot_request_pending = True

    def take_snapshot_request(self) -> bool:
        requested = self._snapshot_request_pending
        self._snapshot_request_pending = False
        return requested

    def transcript(self) -> str:
        return "\n\n".join(f"{message.role}: {message.text}" for message in self._messages)

    def clear_sensitive(self) -> None:
        for message in self._messages:
            message.text = ""
        self._messages.clear()
        self.active_turn_id = None
        self.last_seq = 0
        self.segment_count = 0
        self.snapshot_pending = False
        self._snapshot_request_pending = False
        self._snapshot_id = None
        self._snapshot_chunk_count = 0
        self._next_snapshot_chunk = 0
        self._turn_states.clear()
        self._shown_user_message_ids.clear()

    def _clear_visible_bodies(self) -> None:
        for message in self._messages:
            message.text = ""
        self._messages.clear()
        self._shown_user_message_ids.clear()

    def _append(self, message: _VisibleMessage) -> None:
        self._messages.append(message)
        if len(self._messages) > MAX_VISIBLE_MESSAGES:
            del self._messages[: len(self._messages) - MAX_VISIBLE_MESSAGES]


class MainWindow(QMainWindow):
    def __init__(
        self,
        bridge: ApplicationBridge,
        backend_host: BackendThreadHost,
    ) -> None:
        super().__init__()
        self._bridge = bridge
        self._backend_host = backend_host
        self.model = DesktopViewModel()
        self._pending_message: UserMessage | None = None
        self._pending_command_ids: set[str] = set()
        self._closing = False
        self._allow_close = False
        self.setWindowTitle("Megumin Companion")
        self.resize(720, 560)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        self.message_view = QPlainTextEdit(central)
        self.message_view.setReadOnly(True)
        self.message_view.setPlaceholderText("尚无消息。W13 只验证桌面骨架；真实对话在 W14 接入。")
        self.message_view.setPlaceholderText(
            "\u6682\u65e0\u6d88\u606f\u3002\u6587\u5b57\u5bf9\u8bdd\u5c06\u5728\u6b64\u663e\u793a\u3002"
        )
        self.message_view.setAccessibleName("消息区")
        self.editor = QPlainTextEdit(central)
        self.editor.setPlaceholderText("输入消息；Ctrl+Enter 发送")
        self.editor.setAccessibleName("消息编辑器")
        self.editor.setTabChangesFocus(True)
        self.editor.setMaximumBlockCount(2_000)
        button_row = QHBoxLayout()
        self.send_button = QPushButton("发送", central)
        self.send_button.setAccessibleName("发送消息")
        self.stop_button = QPushButton("停止", central)
        self.stop_button.setAccessibleName("停止当前回复")
        button_row.addStretch(1)
        button_row.addWidget(self.stop_button)
        button_row.addWidget(self.send_button)
        status_row = QHBoxLayout()
        self.connection_status = QLabel("后端：已停止", central)
        self.connection_status.setAccessibleName("后端：已停止")
        self.feature_status = QLabel("文字聊天：待 W14 接入", central)
        self.feature_status.setAccessibleName("文字聊天：待 W14 接入")
        status_row.addWidget(self.connection_status)
        status_row.addStretch(1)
        status_row.addWidget(self.feature_status)
        layout.addWidget(self.message_view, 1)
        layout.addWidget(self.editor)
        layout.addLayout(button_row)
        layout.addLayout(status_row)
        self.setCentralWidget(central)
        QWidget.setTabOrder(self.message_view, self.editor)
        QWidget.setTabOrder(self.editor, self.stop_button)
        QWidget.setTabOrder(self.stop_button, self.send_button)

        self._send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._send_shortcut.activated.connect(self._submit_message)
        self.send_button.clicked.connect(self._submit_message)
        self.stop_button.clicked.connect(self._stop_turn)
        self._bridge.events_available.connect(self._drain_events)
        self._bridge.command_rejected.connect(self._show_error)
        self._backend_host.stopped.connect(self._backend_stopped)
        self._sync_view()

    def _submit_message(self) -> None:
        if not self.model.capabilities.text_chat:
            self._show_error("feature_not_available")
            return
        text = self.editor.toPlainText()
        if text.strip():
            try:
                message = UserMessage(text=text)
            except ValidationError:
                self._show_error("invalid_user_message")
                return
        else:
            pending_message = self._pending_message
            if pending_message is None:
                self._show_error("invalid_user_message")
                return
            message = pending_message
        command = UserMessageCommand(payload=message)
        if self._bridge.submit_command(command):
            self._pending_message = message
            self._pending_command_ids.add(command.command_id)
            self.model.add_user_message(message)
            self.editor.clear()
            self._sync_view()

    def _stop_turn(self) -> None:
        turn_id = self.model.active_turn_id
        if turn_id is None or not self.model.capabilities.turn_cancel:
            return
        request = TurnInterruptRequest(turn_id=turn_id)
        self._bridge.submit_command(TurnCancelCommand(payload=request))

    def _drain_events(self) -> None:
        for event in self._bridge.drain_events():
            self.model.apply_event(event)
            self._settle_pending_message(event)
        if self.model.take_snapshot_request():
            self._bridge.request_snapshot()
        self._sync_view()

    def _settle_pending_message(self, event: BridgeEvent) -> None:
        if isinstance(event, CommandRejectedEvent):
            self._pending_command_ids.discard(event.command_id)
            return
        if not isinstance(event, PipelineEvent) or event.type not in {
            "turn.accepted",
            "turn.snapshot",
        }:
            return
        source_message_id = event.payload.get("source_message_id")
        if (
            self._pending_message is not None
            and source_message_id == self._pending_message.message_id
        ):
            self._pending_message = None
            self._pending_command_ids.clear()

    def _show_error(self, reason_code: str) -> None:
        self.model.last_error_code = reason_code
        self._sync_view()

    def _sync_view(self) -> None:
        transcript = self.model.transcript()
        if self.message_view.toPlainText() != transcript:
            self.message_view.setPlainText(transcript)
            self.message_view.moveCursor(QTextCursor.MoveOperation.End)
        labels = {
            BackendState.starting: "启动中",
            BackendState.ready: "已连接",
            BackendState.restarting: "重启中",
            BackendState.degraded: "已降级",
            BackendState.failed: "失败",
            BackendState.stopping: "停止中",
            BackendState.stopped: "已停止",
        }
        status = f"后端：{labels[self.model.connection_state]}"
        if self.model.last_error_code:
            status = f"{status}（{self.model.last_error_code}）"
        self.connection_status.setText(status)
        self.connection_status.setAccessibleName(status)
        chat_ready = (
            self.model.connection_state is BackendState.ready
            and self.model.capabilities.text_chat
            and not self._closing
        )
        feature_status = "文字聊天：可用" if chat_ready else "文字聊天：待 W14 接入"
        feature_status = _feature_status_text(chat_ready, self.model.connection_state)
        self.feature_status.setText(feature_status)
        self.feature_status.setAccessibleName(feature_status)
        self.send_button.setEnabled(chat_ready)
        self.stop_button.setEnabled(
            chat_ready
            and self.model.capabilities.turn_cancel
            and self.model.active_turn_id is not None
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        if self._allow_close or not self._backend_host.has_active_generation:
            self._wipe_sensitive_state()
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self.model.connection_state = BackendState.stopping
            self._sync_view()
            self._backend_host.request_stop()

    def _backend_stopped(self) -> None:
        if self._closing:
            self._allow_close = True
            self.close()

    def discard_sensitive_state(self) -> None:
        """Best-effort in-memory wipe for non-window application quit paths."""

        self._wipe_sensitive_state()

    def _wipe_sensitive_state(self) -> None:
        self.editor.clear()
        self.message_view.clear()
        self._pending_message = None
        self._pending_command_ids.clear()
        self.model.clear_sensitive()
        self._bridge.clear_sensitive()


def _feature_status_text(available: bool, state: BackendState) -> str:
    if available:
        return "\u6587\u5b57\u804a\u5929\uff1a\u53ef\u7528"
    if state in {BackendState.starting, BackendState.restarting, BackendState.stopping}:
        return "\u6587\u5b57\u804a\u5929\uff1a\u8fde\u63a5\u4e2d"
    return "\u6587\u5b57\u804a\u5929\uff1a\u4e0d\u53ef\u7528"
