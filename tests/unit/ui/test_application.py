from __future__ import annotations

import json
import threading
from collections.abc import Callable
from functools import partial

import pytest
from desktop_client.ui import application
from desktop_client.ui.backend import BackendThreadHost
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import BackendState
from desktop_client.ui.window import MainWindow
from PySide6.QtWidgets import QApplication


def _window_is_ready(window: MainWindow) -> bool:
    return window.model.connection_state is BackendState.ready


def _owner_graph_is_stopped(backend: BackendThreadHost, window: MainWindow) -> bool:
    return not backend.has_active_generation and not window.isVisible()


def test_real_headless_smoke_uses_production_owner_graph(
    qapp: QApplication,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert application.run_headless_smoke(timeout_seconds=2.0) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "backend_ready": True,
        "backend_stopped": True,
        "platform": "offscreen",
        "status": "ok",
    }
    qapp.processEvents()


def test_full_shell_owner_graph_starts_and_closes_one_hundred_times(
    qapp: QApplication,
) -> None:
    for cycle in range(100):
        bridge = ApplicationBridge()
        backend = BackendThreadHost(bridge, auto_restart_limit=0)
        window = MainWindow(bridge, backend)
        window.show()
        assert backend.start(), cycle
        assert application._pump_until(
            qapp,
            partial(_window_is_ready, window),
            1.0,
        ), cycle
        window.editor.setPlainText(f"draft-{cycle}")
        window.close()
        assert application._pump_until(
            qapp,
            partial(_owner_graph_is_stopped, backend, window),
            1.0,
        ), cycle
        assert window.editor.toPlainText() == ""


def test_run_desktop_composes_shell_without_network(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def start(_host: BackendThreadHost) -> bool:
        calls.append("start")
        return True

    def show(_window: MainWindow) -> None:
        calls.append("show")

    monkeypatch.setattr(BackendThreadHost, "start", start)
    monkeypatch.setattr(MainWindow, "show", show)
    monkeypatch.setattr(QApplication, "exec", lambda _app: 17)

    assert application.run_desktop([]) == 17
    assert calls == ["start", "show"]
    assert "uvicorn" not in vars(application)
    qapp.processEvents()


@pytest.mark.parametrize("target", [application.run_desktop, application.run_headless_smoke])
def test_qt_entry_points_reject_non_main_thread(target: Callable[..., int]) -> None:
    failures: list[str] = []

    def invoke() -> None:
        try:
            target()
        except RuntimeError as exc:
            failures.append(str(exc))

    thread = threading.Thread(target=invoke)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert failures and "main thread" in failures[0]


def test_pump_until_reports_timeout(qapp: QApplication) -> None:
    assert not application._pump_until(qapp, lambda: False, 0.001)
