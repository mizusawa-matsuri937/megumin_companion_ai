from __future__ import annotations

from collections.abc import Sequence

from app.schemas import UserMessage
from desktop_client.ui.appearance import ChatMessageView, ChatUiParts
from desktop_client.ui.backend import BackendThreadHost, SkeletonBackendRuntime
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.window import MainWindow
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QWidget


class _RecordingTranscriptSurface:
    def __init__(self, parent: QWidget) -> None:
        self._widget = QPlainTextEdit(parent)
        self._widget.setReadOnly(True)
        self.rendered: list[tuple[ChatMessageView, ...]] = []
        self.clear_count = 0

    @property
    def widget(self) -> QPlainTextEdit:
        return self._widget

    def render(self, messages: Sequence[ChatMessageView]) -> None:
        snapshot = tuple(messages)
        self.rendered.append(snapshot)
        self._widget.setPlainText("|".join(message.role for message in snapshot))

    def clear_sensitive(self) -> None:
        self.clear_count += 1
        self._widget.clear()


class _RecordingAppearance:
    appearance_id = "recording"

    def __init__(self) -> None:
        self.surface: _RecordingTranscriptSurface | None = None
        self.ui: ChatUiParts | None = None

    def create_transcript_surface(self, parent: QWidget) -> _RecordingTranscriptSurface:
        self.surface = _RecordingTranscriptSurface(parent)
        return self.surface

    def apply(self, ui: ChatUiParts) -> None:
        self.ui = ui
        ui.root.setStyleSheet("#chatRoot { padding: 1px; }")


def test_appearance_can_replace_only_the_chat_presentation_surface(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: SkeletonBackendRuntime(),
        auto_restart_limit=0,
    )
    appearance = _RecordingAppearance()
    window = MainWindow(bridge, host, appearance=appearance)

    assert appearance.ui is not None
    assert appearance.surface is not None
    assert appearance.ui.root is window.centralWidget()
    assert appearance.ui.transcript is window.message_view
    assert appearance.ui.editor is window.editor
    assert appearance.ui.send_button is window.send_button
    assert window.message_view.objectName() == "chatTranscript"
    assert window.editor.objectName() == "chatEditor"
    assert window.send_button.objectName() == "chatSendButton"

    message = UserMessage(text="presentation-only sentinel")
    window.model.add_user_message(message)
    window._sync_view()

    assert appearance.surface.rendered[-1] == (
        ChatMessageView(role="user", text="presentation-only sentinel"),
    )
    assert window.model.transcript() == "\u4f60: presentation-only sentinel"

    window.model.clear_sensitive()
    window._sync_view()

    assert appearance.surface.widget.toPlainText() == ""

    window.discard_sensitive_state()

    assert appearance.surface.clear_count == 1
    assert appearance.surface.widget.toPlainText() == ""
    assert window.model.display_messages() == ()
    window.close()
