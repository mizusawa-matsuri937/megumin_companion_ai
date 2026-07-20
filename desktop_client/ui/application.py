"""Desktop application ownership and explicit offscreen smoke entry point."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Sequence

from app import __version__
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from desktop_client.ui.backend import (
    BackendRuntimeFactory,
    BackendThreadHost,
    DesktopChatRuntimeFactory,
)
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import BackendState
from desktop_client.ui.window import MainWindow


def _application(argv: Sequence[str]) -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    if existing is not None:
        raise RuntimeError("a non-GUI QCoreApplication already owns the process")
    app = QApplication(list(argv))
    app.setApplicationName("Megumin Companion")
    app.setApplicationDisplayName("Megumin Companion")
    app.setApplicationVersion(__version__)
    return app


def run_desktop(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: BackendRuntimeFactory | None = None,
) -> int:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Qt desktop must start on the process main thread")
    app = _application(argv or [])
    bridge = ApplicationBridge(parent=app)
    backend = BackendThreadHost(bridge, runtime_factory=runtime_factory, parent=app)
    window = MainWindow(bridge, backend)
    backend.start()
    window.show()
    app.aboutToQuit.connect(backend.request_stop)
    try:
        exit_code = app.exec()
    finally:
        app.aboutToQuit.disconnect(backend.request_stop)
        backend.request_stop()
        backend.wait_for_current_thread(3_000)
        window.discard_sensitive_state()
        app.processEvents()
    return exit_code


def run_headless_smoke(
    *,
    timeout_seconds: float = 5.0,
    runtime_factory: BackendRuntimeFactory | None = None,
) -> int:
    """Start the real shell offscreen, then close it through the production owner graph."""

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Qt smoke must start on the process main thread")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if runtime_factory is None:
        runtime_factory = _headless_runtime_factory()
    app = _application([])
    bridge = ApplicationBridge(parent=app)
    backend = BackendThreadHost(
        bridge,
        runtime_factory=runtime_factory,
        auto_restart_limit=0,
        parent=app,
    )
    window = MainWindow(bridge, backend)
    window.show()
    backend.start()
    ready = _pump_until(
        app,
        lambda: window.model.connection_state is BackendState.ready,
        timeout_seconds,
    )
    window.close()
    stopped = _pump_until(
        app,
        lambda: not backend.has_active_generation and not window.isVisible(),
        timeout_seconds,
    )
    if not stopped:
        backend.request_stop()
        backend.wait_for_current_thread(int(timeout_seconds * 1_000))
    result = {
        "backend_ready": ready,
        "backend_stopped": stopped,
        "platform": app.platformName(),
        "status": "ok" if ready and stopped else "failed",
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if ready and stopped else 4


def _headless_runtime_factory() -> BackendRuntimeFactory:
    """Use the normal W14 composition without mixing application logs into smoke JSON."""

    from app.config import load_settings
    from app.main import create_app

    settings = load_settings()
    settings = settings.model_copy(
        update={
            "logging": settings.logging.model_copy(update={"console_enabled": False}),
        }
    )
    return DesktopChatRuntimeFactory(app_factory=lambda: create_app(settings))


def _pump_until(
    app: QCoreApplication,
    predicate: Callable[[], bool],
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.001)
    app.processEvents()
    return bool(predicate())
