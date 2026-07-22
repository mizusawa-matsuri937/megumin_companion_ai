"""Pure presentation extension points for the desktop chat window.

Appearance implementations may customize Widgets and replace the transcript
surface, but they must not read or write backend state, files, settings, or
chat persistence. Message bodies remain in memory and every surface must
clear its own rendered state when ``clear_sensitive`` is called.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ChatMessageRole = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessageView:
    """Immutable, in-memory-only message data made available to renderers."""

    role: ChatMessageRole
    text: str
    turn_id: str | None = None


class ChatTranscriptSurface(Protocol):
    """A replaceable visual surface for the currently visible transcript."""

    @property
    def widget(self) -> QWidget:
        """Return the focusable root Widget mounted in the chat layout."""

    def render(self, messages: Sequence[ChatMessageView]) -> None:
        """Render a complete immutable snapshot; an empty one clears old text."""

    def clear_sensitive(self) -> None:
        """Clear every message body retained by the surface."""


@dataclass(frozen=True, slots=True)
class ChatUiParts:
    """Widget-only handles available to an appearance implementation.

    This deliberately excludes the bridge, backend host, models, settings, and
    persistence services so an appearance remains a presentation-only concern.
    """

    root: QWidget
    main_layout: QVBoxLayout
    action_layout: QHBoxLayout
    status_layout: QHBoxLayout
    transcript: QWidget
    editor: QPlainTextEdit
    send_button: QPushButton
    stop_button: QPushButton
    voice_button: QPushButton
    settings_button: QPushButton
    connection_status: QLabel
    feature_status: QLabel
    lifecycle_status: QLabel


class ChatAppearance(Protocol):
    """Contract for an opt-in desktop chat appearance.

    Implementations can use object names and ``ChatUiParts`` to apply QSS,
    fonts, spacing, and other Widget-only polish. They may also return a
    different transcript surface for layouts such as message bubbles.
    """

    appearance_id: str

    def create_transcript_surface(self, parent: QWidget) -> ChatTranscriptSurface:
        """Create the transcript surface for one ``MainWindow`` instance."""

    def apply(self, ui: ChatUiParts) -> None:
        """Apply visual-only customization after the window Widgets exist."""


def plain_text_transcript(messages: Sequence[ChatMessageView]) -> str:
    """Render the legacy plain-text transcript without leaking it elsewhere."""

    labels: dict[ChatMessageRole, str] = {
        "user": "\u4f60",
        "assistant": "\u52a9\u624b",
    }
    return "\n\n".join(f"{labels[message.role]}: {message.text}" for message in messages)


class PlainTextTranscriptSurface:
    """The default surface, preserving the current QPlainTextEdit behavior."""

    def __init__(self, parent: QWidget) -> None:
        self._widget = QPlainTextEdit(parent)
        self._widget.setReadOnly(True)
        self._widget.setPlaceholderText(
            "\u6682\u65e0\u6d88\u606f\u3002\u6587\u5b57\u5bf9\u8bdd\u5c06\u5728\u6b64\u663e\u793a\u3002"
        )

    @property
    def widget(self) -> QPlainTextEdit:
        return self._widget

    def render(self, messages: Sequence[ChatMessageView]) -> None:
        transcript = plain_text_transcript(messages)
        if self._widget.toPlainText() != transcript:
            self._widget.setPlainText(transcript)
            self._widget.moveCursor(QTextCursor.MoveOperation.End)

    def clear_sensitive(self) -> None:
        self._widget.clear()


@dataclass(frozen=True, slots=True)
class DefaultChatAppearance:
    """No-op appearance used when callers do not request a custom theme."""

    appearance_id: str = "default"

    def create_transcript_surface(self, parent: QWidget) -> PlainTextTranscriptSurface:
        return PlainTextTranscriptSurface(parent)

    def apply(self, ui: ChatUiParts) -> None:
        del ui
