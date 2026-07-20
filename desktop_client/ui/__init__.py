"""W13 PySide6 desktop shell and bounded in-process application bridge."""

from desktop_client.ui.application import run_desktop, run_headless_smoke
from desktop_client.ui.backend import BackendContext, BackendThreadHost, SkeletonBackendRuntime
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    BackendState,
    BackendStateEvent,
    BridgeCommand,
    BridgeEvent,
    TurnCancelCommand,
    UserMessageCommand,
)
from desktop_client.ui.window import DesktopViewModel, MainWindow

__all__ = [
    "ApplicationBridge",
    "BackendCapabilities",
    "BackendContext",
    "BackendState",
    "BackendStateEvent",
    "BackendThreadHost",
    "BridgeCommand",
    "BridgeEvent",
    "DesktopViewModel",
    "MainWindow",
    "SkeletonBackendRuntime",
    "TurnCancelCommand",
    "UserMessageCommand",
    "run_desktop",
    "run_headless_smoke",
]
