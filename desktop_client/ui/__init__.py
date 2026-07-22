"""W13/W14 PySide6 desktop shell and bounded in-process application bridge."""

from desktop_client.ui.appearance import (
    ChatAppearance,
    ChatMessageRole,
    ChatMessageView,
    ChatTranscriptSurface,
    ChatUiParts,
    DefaultChatAppearance,
)
from desktop_client.ui.application import run_desktop, run_headless_smoke
from desktop_client.ui.backend import (
    BackendContext,
    BackendThreadHost,
    DesktopChatRuntime,
    DesktopChatRuntimeFactory,
    DesktopSessionCursor,
    SkeletonBackendRuntime,
    TurnServiceBackendRuntime,
)
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    BackendState,
    BackendStateEvent,
    BridgeCommand,
    BridgeEvent,
    TurnCancelCommand,
    UserMessageCommand,
    VoiceCancelCommand,
    VoiceStartCommand,
    VoiceStateEvent,
    VoiceStopCommand,
    VoiceUserMessageEvent,
)
from desktop_client.ui.window import DesktopViewModel, MainWindow

__all__ = [
    "ApplicationBridge",
    "ChatAppearance",
    "ChatMessageRole",
    "ChatMessageView",
    "ChatTranscriptSurface",
    "ChatUiParts",
    "BackendCapabilities",
    "BackendContext",
    "BackendState",
    "BackendStateEvent",
    "BackendThreadHost",
    "BridgeCommand",
    "BridgeEvent",
    "DesktopViewModel",
    "DesktopChatRuntime",
    "DesktopChatRuntimeFactory",
    "DesktopSessionCursor",
    "DefaultChatAppearance",
    "MainWindow",
    "SkeletonBackendRuntime",
    "TurnCancelCommand",
    "TurnServiceBackendRuntime",
    "UserMessageCommand",
    "VoiceCancelCommand",
    "VoiceStartCommand",
    "VoiceStateEvent",
    "VoiceStopCommand",
    "VoiceUserMessageEvent",
    "run_desktop",
    "run_headless_smoke",
]
