from __future__ import annotations

import os
import time
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from app.paths import AppPathError, AppPaths
from desktop_client.lifecycle import CrashMarker, DesktopLifecycle, LifecycleState
from desktop_client.single_instance import (
    InstanceRole,
    PortableCurrentSessionInstance,
    WindowsCurrentUserInstance,
    recovery_marker_scope,
    windows_instance_name,
)
from desktop_client.startup import UnsupportedStartupRegistration
from desktop_client.ui import tray as tray_module
from desktop_client.ui.application import run_desktop
from desktop_client.ui.backend import SkeletonBackendRuntime
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import BackendCapabilities, BackendState
from desktop_client.ui.tray import DesktopTrayController
from desktop_client.ui.window import MainWindow
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLabel


def _pump_until(qapp: QApplication, predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.001)
    qapp.processEvents()
    return bool(predicate())


class _FakeBackend(QObject):
    stopped = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.start_calls = 0
        self.stop_calls = 0
        self.wait_result = True

    @property
    def has_active_generation(self) -> bool:
        return self.active

    def start(self) -> bool:
        self.start_calls += 1
        self.active = True
        return True

    def request_stop(self) -> None:
        self.stop_calls += 1

    def wait_for_current_thread(self, _timeout_ms: int) -> bool:
        return self.wait_result

    def finish(self) -> None:
        self.active = False
        self.stopped.emit()


class _FakeTray:
    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.show_calls = 0
        self.shutdown_calls = 0

    def show(self) -> None:
        self.show_calls += 1

    def prepare_shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeSignal:
    def __init__(self) -> None:
        self._callbacks: list[Callable[..., None]] = []

    def connect(self, callback: Callable[..., None]) -> None:
        self._callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in tuple(self._callbacks):
            callback(*args)


class _FakeAction:
    def __init__(self, text: str) -> None:
        self.text = text
        self.triggered = _FakeSignal()
        self.enabled = True

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt API spelling
        self.enabled = enabled


class _FakeMenu:
    latest: ClassVar[_FakeMenu | None] = None

    def __init__(self, parent: object | None = None) -> None:
        self.parent = parent
        self.actions: list[_FakeAction] = []
        _FakeMenu.latest = self

    def addAction(self, text: str) -> _FakeAction:  # noqa: N802 - Qt API spelling
        action = _FakeAction(text)
        self.actions.append(action)
        return action

    def addSeparator(self) -> None:  # noqa: N802 - Qt API spelling
        return None


class _FakeActivationReason(IntEnum):
    Trigger = 1
    DoubleClick = 2
    Context = 3


class _FakeSystemTrayIcon:
    ActivationReason = _FakeActivationReason
    latest: ClassVar[_FakeSystemTrayIcon | None] = None

    def __init__(self, parent: object | None = None) -> None:
        self.parent = parent
        self.activated = _FakeSignal()
        self.visible = False
        self.show_calls = 0
        self.context_menu: _FakeMenu | None = None
        _FakeSystemTrayIcon.latest = self

    @staticmethod
    def isSystemTrayAvailable() -> bool:  # noqa: N802 - Qt API spelling
        return True

    def setObjectName(self, _name: str) -> None:  # noqa: N802 - Qt API spelling
        return None

    def setIcon(self, _icon: object) -> None:  # noqa: N802 - Qt API spelling
        return None

    def setToolTip(self, _tooltip: str) -> None:  # noqa: N802 - Qt API spelling
        return None

    def setContextMenu(self, menu: _FakeMenu) -> None:  # noqa: N802 - Qt API spelling
        self.context_menu = menu

    def show(self) -> None:
        self.visible = True
        self.show_calls += 1

    def hide(self) -> None:
        self.visible = False


def _started_marker(tmp_path: Path) -> CrashMarker:
    state = tmp_path / "state"
    state.mkdir()
    marker = CrashMarker(state / "desktop-crash-marker-v1.json")
    assert not marker.begin().safe_mode
    return marker


def test_crash_marker_marks_unclean_previous_exit_as_safe_mode(tmp_path: Path) -> None:
    marker = _started_marker(tmp_path)
    assert marker.path.read_text(encoding="utf-8") == '{"schema_version":1,"state":"running"}'

    restarted = CrashMarker(marker.path)
    recovery = restarted.begin()
    assert recovery.safe_mode
    assert recovery.reason_code == "previous_shutdown_unclean"
    assert restarted.finish_normal()
    assert not restarted.path.exists()

    marker.path.write_text("not-json", encoding="utf-8")
    invalid = CrashMarker(marker.path).begin()
    assert invalid.safe_mode
    assert invalid.reason_code == "crash_marker_invalid"


def test_portable_single_instance_limits_secondary_to_show_and_isolates_sessions() -> None:
    primary = PortableCurrentSessionInstance(instance_id="W15Test", session_key="session-a")
    secondary = PortableCurrentSessionInstance(instance_id="W15Test", session_key="session-a")
    other_session = PortableCurrentSessionInstance(instance_id="W15Test", session_key="session-b")
    try:
        assert primary.acquire() is InstanceRole.primary
        assert secondary.acquire() is InstanceRole.secondary
        assert secondary.request_show()
        assert primary.take_show_request()
        assert not primary.take_show_request()

        # A distinct session owns a separate Local namespace instance instead
        # of routing a cross-session request into the first window.
        assert other_session.acquire() is InstanceRole.primary
        assert primary.recovery_marker_scope == secondary.recovery_marker_scope
        assert primary.recovery_marker_scope != other_session.recovery_marker_scope
        assert "session-a" not in primary.recovery_marker_scope
        scoped_marker = AppPaths(root=Path("C:/w15-test")).desktop_crash_marker_for_scope(
            primary.recovery_marker_scope
        )
        assert scoped_marker.name.startswith("desktop-crash-marker-v1-")
        assert "session-a" not in scoped_marker.name
        with pytest.raises(AppPathError, match="scope is invalid"):
            AppPaths(root=Path("C:/w15-test")).desktop_crash_marker_for_scope("session-a")
        name = windows_instance_name(instance_id="W15Test", user_sid="S-1-5-21-example")
        assert name.startswith("Local\\W15Test.")
        assert "S-1-5-21-example" not in name
        assert recovery_marker_scope(
            user_sid="S-1-5-21-example", session_id=7
        ) != recovery_marker_scope(user_sid="S-1-5-21-example", session_id=8)
    finally:
        secondary.close()
        other_session.close()
        primary.close()


def test_windows_single_instance_current_user_event_boundary() -> None:
    if os.name != "nt":
        return
    instance_id = "W15Native" + uuid4().hex
    primary = WindowsCurrentUserInstance.for_current_user(instance_id=instance_id)
    secondary = WindowsCurrentUserInstance.for_current_user(instance_id=instance_id)
    try:
        assert primary.acquire() is InstanceRole.primary
        assert secondary.acquire() is InstanceRole.secondary
        assert secondary.request_show()
        assert primary.take_show_request()
        assert not primary.take_show_request()
    finally:
        secondary.close()
        primary.close()


def test_lifecycle_hides_window_then_performs_ordered_normal_exit(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    bridge = ApplicationBridge()
    backend = _FakeBackend()
    window = MainWindow(bridge, backend)  # type: ignore[arg-type]
    tray = _FakeTray(available=True)
    marker = _started_marker(tmp_path)
    instance = PortableCurrentSessionInstance(instance_id="W15Lifecycle", session_key="normal")
    assert instance.acquire() is InstanceRole.primary
    lifecycle = DesktopLifecycle(
        qapp,
        backend,  # type: ignore[arg-type]
        window,
        tray,  # type: ignore[arg-type]
        instance,
        marker,
        safe_mode=False,
        shutdown_deadline_ms=1_000,
        hard_exit=lambda _code: (_ for _ in ()).throw(AssertionError("unexpected hard exit")),
    )
    window.show()
    assert lifecycle.start()
    assert backend.start_calls == 1 and tray.show_calls == 1

    # W15's explicit default is close-to-tray while the primary is running.
    window.close()
    qapp.processEvents()
    assert not window.isVisible()
    assert lifecycle.state is LifecycleState.running
    assert backend.stop_calls == 0

    assert lifecycle.request_exit()
    assert not lifecycle.request_exit()
    assert backend.stop_calls == 1
    backend.finish()
    assert _pump_until(qapp, lambda: lifecycle.state is LifecycleState.stopped)
    assert not marker.path.exists()
    assert tray.shutdown_calls >= 1
    assert window.editor.toPlainText() == ""


def test_tray_exposes_only_fixed_actions_and_privacy_overview(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tray_module, "QSystemTrayIcon", _FakeSystemTrayIcon)
    monkeypatch.setattr(tray_module, "QMenu", _FakeMenu)
    bridge = ApplicationBridge()
    backend = _FakeBackend()
    window = MainWindow(bridge, backend)  # type: ignore[arg-type]
    calls: list[str] = []
    monkeypatch.setattr(window, "show_and_activate", lambda: calls.append("show"))
    monkeypatch.setattr(window, "hide", lambda: calls.append("hide"))
    monkeypatch.setattr(window, "stop_current_turn", lambda: calls.append("stop"))
    controller = DesktopTrayController(
        qapp,
        window,
        on_exit=lambda: calls.append("exit"),
    )
    assert controller.available
    controller.show()
    tray = _FakeSystemTrayIcon.latest
    menu = _FakeMenu.latest
    assert tray is not None and tray.show_calls >= 1
    assert menu is not None
    assert [action.text for action in menu.actions] == [
        "显示窗口",
        "隐藏窗口",
        "停止当前回复",
        "隐私总览",
        "退出",
    ]
    menu.actions[0].triggered.emit(False)
    menu.actions[1].triggered.emit(False)
    menu.actions[2].triggered.emit(False)
    menu.actions[3].triggered.emit(False)
    menu.actions[4].triggered.emit(False)
    qapp.processEvents()
    assert calls == ["show", "hide", "stop", "exit"]
    dialog = controller._privacy_dialog  # noqa: SLF001 - fixed disclosure surface under test
    assert dialog is not None
    # The non-modal dialog is intentionally static and contains no transcript.
    assert dialog.windowTitle() == "隐私总览"
    privacy_labels = dialog.findChildren(QLabel)
    assert any("不显示消息、密钥、文件路径或服务地址" in label.text() for label in privacy_labels)
    assert dialog.isVisible()
    controller.prepare_shutdown()
    assert not dialog.isVisible()
    assert not tray.visible

    window.model.connection_state = BackendState.ready
    window.model.capabilities = BackendCapabilities(text_chat=True, turn_cancel=True)
    window.model.active_turn_id = "turn-tray"
    controller = DesktopTrayController(qapp, window, on_exit=lambda: None)
    controller.show()
    assert _FakeMenu.latest is not None
    assert _FakeMenu.latest.actions[2].enabled


def test_lifecycle_deadline_keeps_crash_marker_and_uses_hard_exit(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    bridge = ApplicationBridge()
    backend = _FakeBackend()
    window = MainWindow(bridge, backend)  # type: ignore[arg-type]
    tray = _FakeTray(available=False)
    marker = _started_marker(tmp_path)
    instance = PortableCurrentSessionInstance(instance_id="W15Lifecycle", session_key="hang")
    assert instance.acquire() is InstanceRole.primary
    exits: list[int] = []
    lifecycle = DesktopLifecycle(
        qapp,
        backend,  # type: ignore[arg-type]
        window,
        tray,  # type: ignore[arg-type]
        instance,
        marker,
        safe_mode=False,
        shutdown_deadline_ms=1,
        hard_exit=exits.append,
    )
    lifecycle.start()
    assert lifecycle.request_exit()
    assert _pump_until(qapp, lambda: exits == [1])
    assert lifecycle.state is LifecycleState.timed_out
    assert marker.path.exists()
    assert "安全模式" in window.lifecycle_status.text()
    # The injected hard-exit callback intentionally does not exit this test process.
    instance.close()


class _SecondaryInstance:
    def __init__(self) -> None:
        self.closed = False
        self.show_requests = 0

    def acquire(self) -> InstanceRole:
        return InstanceRole.secondary

    def request_show(self) -> bool:
        self.show_requests += 1
        return True

    def take_show_request(self) -> bool:
        return False

    @property
    def recovery_marker_scope(self) -> str:
        return "0" * 32

    def close(self) -> None:
        self.closed = True


def test_secondary_desktop_run_only_signals_show_without_creating_app_state(tmp_path: Path) -> None:
    secondary = _SecondaryInstance()
    missing_root = tmp_path / "not-created"
    assert run_desktop(app_paths=AppPaths(root=missing_root), instance=secondary) == 0
    assert secondary.show_requests == 1 and secondary.closed
    assert not missing_root.exists()


def test_primary_desktop_run_clears_its_session_marker_after_external_quit(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    paths = AppPaths(root=tmp_path / "MeguminCompanion")
    instance = PortableCurrentSessionInstance(instance_id="W15Run", session_key="primary")
    QTimer.singleShot(20, qapp.quit)

    assert (
        run_desktop(
            app_paths=paths,
            instance=instance,
            runtime_factory=lambda _generation: SkeletonBackendRuntime(),
            startup_registration=UnsupportedStartupRegistration(),
            shutdown_deadline_ms=1_000,
            hard_exit=lambda code: pytest.fail(f"unexpected hard exit: {code}"),
        )
        == 0
    )
    assert not paths.desktop_crash_marker_for_scope(instance.recovery_marker_scope).exists()
