"""W15 process-level lifecycle, crash recovery marker, and shutdown ownership."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from desktop_client.single_instance import SingleInstance

if TYPE_CHECKING:
    from desktop_client.ui.backend import BackendThreadHost
    from desktop_client.ui.tray import DesktopTrayController
    from desktop_client.ui.window import MainWindow

_CRASH_MARKER_SCHEMA_VERSION = 1
_MAX_CRASH_MARKER_BYTES = 1_024


class CrashMarkerError(RuntimeError):
    """A content-free failure to maintain the desktop recovery marker."""


@dataclass(frozen=True, slots=True)
class CrashRecovery:
    safe_mode: bool
    reason_code: str | None = None


class CrashMarker:
    """Persist only whether the previous desktop lifetime reached normal exit."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def begin(self) -> CrashRecovery:
        """Classify the previous lifetime, then atomically mark this one running."""

        recovery = self._previous_recovery()
        self._write_running()
        return recovery

    def finish_normal(self) -> bool:
        """Remove the marker only after all owned desktop resources have stopped."""

        try:
            self._assert_regular_or_missing()
            self._path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def _previous_recovery(self) -> CrashRecovery:
        try:
            self._assert_regular_or_missing()
        except OSError:
            # The marker is fail-closed: inability to inspect it means do not
            # restore potentially sensitive automated features.
            return CrashRecovery(safe_mode=True, reason_code="crash_marker_unavailable")
        try:
            payload = self._path.read_bytes()
        except FileNotFoundError:
            return CrashRecovery(safe_mode=False)
        except OSError:
            return CrashRecovery(safe_mode=True, reason_code="crash_marker_unavailable")
        if len(payload) > _MAX_CRASH_MARKER_BYTES:
            return CrashRecovery(safe_mode=True, reason_code="crash_marker_invalid")
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return CrashRecovery(safe_mode=True, reason_code="crash_marker_invalid")
        if not isinstance(parsed, dict) or parsed != {
            "schema_version": _CRASH_MARKER_SCHEMA_VERSION,
            "state": "running",
        }:
            return CrashRecovery(safe_mode=True, reason_code="crash_marker_invalid")
        return CrashRecovery(safe_mode=True, reason_code="previous_shutdown_unclean")

    def _write_running(self) -> None:
        parent = self._path.parent
        if not parent.is_dir():
            raise CrashMarkerError("crash_marker_directory_unavailable")
        try:
            self._assert_regular_or_missing()
        except OSError as exc:
            raise CrashMarkerError("crash_marker_unavailable") from exc
        temporary = parent / f".{self._path.name}.{uuid4().hex}.tmp"
        payload = json.dumps(
            {"schema_version": _CRASH_MARKER_SCHEMA_VERSION, "state": "running"},
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            raise CrashMarkerError("crash_marker_write_failed") from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _assert_regular_or_missing(self) -> None:
        try:
            marker_stat = self._path.lstat()
        except FileNotFoundError:
            return
        if self._path.is_symlink() or not stat.S_ISREG(marker_stat.st_mode):
            raise OSError("crash marker is not a regular file")


class LifecycleState(StrEnum):
    starting = "starting"
    running = "running"
    stopping = "stopping"
    timed_out = "timed_out"
    stopped = "stopped"


class DesktopLifecycle(QObject):
    """The sole process-level owner for UI shutdown, backend stop, and recovery.

    The backend's FastAPI lifespan remains the owner of TurnService, workers,
    VTS, memory, and logging.  This Qt-side owner drives that lifecycle to a
    bounded terminal outcome and only clears the crash marker after it reports
    completion.  A deadline never claims that a stuck Python thread was
    stopped; production instead terminates the process and leaves the marker
    behind for the next safe-mode startup.
    """

    state_changed = Signal(str)

    def __init__(
        self,
        app: QCoreApplication,
        backend: BackendThreadHost,
        window: MainWindow,
        tray: DesktopTrayController,
        instance: SingleInstance,
        marker: CrashMarker,
        *,
        safe_mode: bool,
        shutdown_deadline_ms: int = 5_000,
        activation_poll_ms: int = 200,
        hard_exit: Callable[[int], None] = os._exit,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent or app)
        if not 1 <= shutdown_deadline_ms <= 30_000:
            raise ValueError("shutdown deadline must be between 1 and 30000 milliseconds")
        if not 10 <= activation_poll_ms <= 5_000:
            raise ValueError("activation poll interval must be between 10 and 5000 milliseconds")
        self._app = app
        self._backend = backend
        self._window = window
        self._tray = tray
        self._instance = instance
        self._marker = marker
        self._safe_mode = safe_mode
        self._hard_exit = hard_exit
        self._state = LifecycleState.starting
        self._shutdown_deadline = QTimer(self)
        self._shutdown_deadline.setSingleShot(True)
        self._shutdown_deadline.setInterval(shutdown_deadline_ms)
        self._shutdown_deadline.timeout.connect(self._on_shutdown_deadline)
        self._activation_timer = QTimer(self)
        self._activation_timer.setInterval(activation_poll_ms)
        self._activation_timer.timeout.connect(self._drain_show_request)
        self._backend.stopped.connect(self._on_backend_stopped)
        self._window.set_close_request_handler(self._on_window_close)
        self._app.aboutToQuit.connect(self._on_about_to_quit)

    @property
    def state(self) -> LifecycleState:
        return self._state

    def start(self) -> bool:
        if self._state is not LifecycleState.starting:
            return False
        self._window.set_safe_mode(self._safe_mode)
        self._tray.show()
        self._activation_timer.start()
        self._set_state(LifecycleState.running)
        started = self._backend.start()
        if not started:
            self._window.set_lifecycle_notice("后端启动未完成，正在退出")
            self.request_exit()
        return started

    def request_exit(self) -> bool:
        """Start idempotent ordered shutdown; repeated exit requests are harmless."""

        if self._state in {LifecycleState.stopped, LifecycleState.timed_out}:
            return False
        if self._state is LifecycleState.stopping:
            return False
        self._set_state(LifecycleState.stopping)
        self._activation_timer.stop()
        self._tray.prepare_shutdown()
        self._window.begin_lifecycle_shutdown()
        self._backend.request_stop()
        # A same-thread ``stopped`` signal may complete shutdown synchronously
        # above.  Defer arming the deadline so that path cannot leave a timer
        # running after the marker has been cleared.
        QTimer.singleShot(0, self._arm_deadline_or_complete)
        return True

    def finish_after_event_loop(self, *, timeout_ms: int = 3_000) -> bool:
        """Best-effort finalization when Qt exits through an external OS path.

        This method deliberately leaves the crash marker behind if the backend
        cannot stop within the finite wait.  It is used after ``app.exec`` has
        returned, when queued Qt ``stopped`` signals may no longer be pumped.
        """

        if timeout_ms < 0:
            raise ValueError("final lifecycle wait cannot be negative")
        if self._state is LifecycleState.running:
            self.request_exit()
        if self._state is LifecycleState.stopping:
            stopped = self._backend.wait_for_current_thread(timeout_ms)
            self._app.processEvents()
            if stopped and self._state is LifecycleState.stopping:
                self._complete_shutdown()
            elif self._state is LifecycleState.stopping:
                self._shutdown_deadline.stop()
                self._activation_timer.stop()
                self._tray.prepare_shutdown()
                self._window.discard_sensitive_state()
                self._instance.close()
        return self._state is LifecycleState.stopped

    def _on_window_close(self) -> None:
        if self._state is LifecycleState.running and self._tray.available:
            self._window.hide()
            self._window.set_lifecycle_notice("窗口已隐藏到系统托盘")
            return
        self.request_exit()

    def _drain_show_request(self) -> None:
        try:
            requested = self._instance.take_show_request()
        except Exception:
            # Do not report implementation-specific native details to the UI;
            # the running primary remains safe and usable.
            return
        if requested and self._state is LifecycleState.running:
            self._window.show_and_activate()

    def _on_backend_stopped(self) -> None:
        if self._state is LifecycleState.stopping:
            self._complete_shutdown()

    def _arm_deadline_or_complete(self) -> None:
        if self._state is not LifecycleState.stopping:
            return
        if self._backend.has_active_generation:
            self._shutdown_deadline.start()
        else:
            self._complete_shutdown()

    def _complete_shutdown(self) -> None:
        if self._state is not LifecycleState.stopping:
            return
        self._shutdown_deadline.stop()
        self._activation_timer.stop()
        self._tray.prepare_shutdown()
        self._window.allow_final_close()
        self._window.close()
        self._window.discard_sensitive_state()
        self._marker.finish_normal()
        self._instance.close()
        self._set_state(LifecycleState.stopped)
        self._app.quit()

    def _on_shutdown_deadline(self) -> None:
        if self._state is not LifecycleState.stopping:
            return
        self._set_state(LifecycleState.timed_out)
        self._activation_timer.stop()
        self._tray.prepare_shutdown()
        self._window.set_lifecycle_notice("退出超时；下次将以安全模式启动")
        self._window.discard_sensitive_state()
        # The marker remains present.  Native workers are already owned by
        # W12 Job Objects; this process-level exit is the only hard boundary
        # available when a Python/QThread lifetime itself cannot be joined.
        self._hard_exit(1)

    def _on_about_to_quit(self) -> None:
        if self._state is LifecycleState.running:
            # Qt/Windows can request a quit outside the window close path.
            # We cannot veto the OS here, so retain the marker unless orderly
            # shutdown completes before the process exits.
            self.request_exit()

    def _set_state(self, state: LifecycleState) -> None:
        if self._state is state:
            return
        self._state = state
        self.state_changed.emit(state.value)
