"""Desktop application ownership and explicit offscreen smoke entry point."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Sequence

from app import __version__
from app.config import ConfigurationError, load_settings
from app.main import create_app
from app.paths import AppPaths
from app.runtime_storage import prepare_runtime_storage
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from desktop_client.lifecycle import CrashMarker, DesktopLifecycle
from desktop_client.single_instance import (
    InstanceRole,
    SingleInstance,
    single_instance_for_current_platform,
)
from desktop_client.startup import (
    StartupRegistration,
    StartupRegistrationError,
    UnsupportedStartupRegistration,
    desktop_startup_command,
    startup_registration_for_current_platform,
)
from desktop_client.ui.backend import (
    BackendRuntimeFactory,
    BackendThreadHost,
    DesktopChatRuntimeFactory,
)
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import BackendState
from desktop_client.ui.tray import DesktopTrayController
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
    app_paths: AppPaths | None = None,
    instance: SingleInstance | None = None,
    startup_registration: StartupRegistration | UnsupportedStartupRegistration | None = None,
    shutdown_deadline_ms: int = 5_000,
    hard_exit: Callable[[int], None] | None = None,
) -> int:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Qt desktop must start on the process main thread")
    desktop_instance = instance or single_instance_for_current_platform()
    role = desktop_instance.acquire()
    if role is InstanceRole.secondary:
        delivered = desktop_instance.request_show()
        desktop_instance.close()
        return 0 if delivered else 3

    # Do not even discover/create the per-user storage tree until the primary
    # role is known.  A secondary process owns exactly one action: show the
    # existing window.
    try:
        paths = app_paths or AppPaths.discover()
        app = _application(argv or [])
    except Exception:
        # The primary handle is already published.  If desktop bootstrap
        # cannot continue, release it rather than making a later launch look
        # like an active instance in this still-running interpreter.
        desktop_instance.close()
        raise
    lifecycle: DesktopLifecycle | None = None
    previous_quit_on_last_window_closed: bool | None = None
    try:
        # The secondary path above performs no filesystem work.  The primary
        # alone creates the protected state tree and writes its recovery marker.
        prepare_runtime_storage(paths)
        marker = CrashMarker(
            paths.desktop_crash_marker_for_scope(desktop_instance.recovery_marker_scope)
        )
        recovery = marker.begin()
        resolved_runtime_factory, startup_enabled = _desktop_runtime_factory(
            safe_mode=recovery.safe_mode,
            runtime_factory=runtime_factory,
        )
        startup_error = False
        try:
            registration = startup_registration or startup_registration_for_current_platform()
            registration.reconcile(
                enabled=startup_enabled,
                command=desktop_startup_command(),
            )
        except StartupRegistrationError:
            # The app remains usable; W16 will surface the setting and stable
            # status.  Do not turn a disabled-by-default convenience feature
            # into a startup blocker or expose registry implementation detail.
            startup_error = True

        bridge = ApplicationBridge(parent=app)
        backend = BackendThreadHost(bridge, runtime_factory=resolved_runtime_factory, parent=app)
        window = MainWindow(bridge, backend)
        lifecycle_holder: list[DesktopLifecycle] = []

        def request_exit_from_tray() -> None:
            if lifecycle_holder:
                lifecycle_holder[0].request_exit()

        tray = DesktopTrayController(app, window, on_exit=request_exit_from_tray, parent=app)
        lifecycle = DesktopLifecycle(
            app,
            backend,
            window,
            tray,
            desktop_instance,
            marker,
            safe_mode=recovery.safe_mode,
            shutdown_deadline_ms=shutdown_deadline_ms,
            hard_exit=hard_exit or os._exit,
            parent=app,
        )
        lifecycle_holder.append(lifecycle)
        if tray.available:
            previous_quit_on_last_window_closed = app.quitOnLastWindowClosed()
            app.setQuitOnLastWindowClosed(False)
        if startup_error:
            window.set_lifecycle_notice("启动项未更新")
        lifecycle.start()
        window.show()
        exit_code = app.exec()
    finally:
        if lifecycle is not None:
            lifecycle.finish_after_event_loop()
        else:
            desktop_instance.close()
        if previous_quit_on_last_window_closed is not None:
            app.setQuitOnLastWindowClosed(previous_quit_on_last_window_closed)
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

    settings = load_settings()
    settings = settings.model_copy(
        update={
            "logging": settings.logging.model_copy(update={"console_enabled": False}),
        }
    )
    return DesktopChatRuntimeFactory(app_factory=lambda: create_app(settings))


def _desktop_runtime_factory(
    *,
    safe_mode: bool,
    runtime_factory: BackendRuntimeFactory | None,
) -> tuple[BackendRuntimeFactory, bool]:
    """Load desktop policy once without turning config errors into UI-thread leaks."""

    if runtime_factory is not None:
        return runtime_factory, False
    try:
        settings = load_settings()
    except ConfigurationError:
        # Keep W14's body-safe degraded runtime behavior: the backend will
        # report an unavailable state through its normal composition boundary.
        return DesktopChatRuntimeFactory(app_factory=lambda: create_app(safe_mode=safe_mode)), False
    return (
        DesktopChatRuntimeFactory(app_factory=lambda: create_app(settings, safe_mode=safe_mode)),
        settings.desktop.startup_enabled,
    )


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
