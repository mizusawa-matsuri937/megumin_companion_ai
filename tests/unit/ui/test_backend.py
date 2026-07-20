from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from app.schemas import UserMessage
from desktop_client.ui import backend as backend_module
from desktop_client.ui.backend import BackendContext, BackendThreadHost, SkeletonBackendRuntime
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    BackendState,
    BackendStateEvent,
    CommandRejectedEvent,
    UserMessageCommand,
)
from PySide6.QtWidgets import QApplication


def _pump_until(
    app: QApplication,
    predicate: Callable[[], bool],
    *,
    timeout: float = 3.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.001)
    app.processEvents()
    return predicate()


def _states(bridge: ApplicationBridge) -> list[BackendStateEvent]:
    return [event for event in bridge.drain_events() if isinstance(event, BackendStateEvent)]


def test_backend_thread_starts_and_stops_one_hundred_times(qapp: QApplication) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: SkeletonBackendRuntime(),
        auto_restart_limit=0,
    )

    for cycle in range(100):
        assert host.start()
        assert not host.start()
        assert _pump_until(
            qapp,
            lambda: any(event.state is BackendState.ready for event in _states(bridge)),
        ), cycle
        host.request_stop()
        assert _pump_until(qapp, lambda: not host.has_active_generation), cycle
        assert host.wait_for_current_thread(50)
        states = _states(bridge)
        assert any(event.state is BackendState.stopped for event in states)

    assert host.generation == 100
    assert host.retired_thread_count <= 4


def test_backend_crash_is_body_safe_and_restarts_once(qapp: QApplication) -> None:
    bridge = ApplicationBridge()

    class Runtime:
        def __init__(self, generation: int) -> None:
            self._generation = generation

        async def run(self, context: BackendContext) -> None:
            if self._generation == 1:
                raise RuntimeError("PRIVATE_BACKEND_EXCEPTION_BODY")
            context.bridge.publish_event(
                BackendStateEvent(
                    generation=context.generation,
                    state=BackendState.ready,
                    capabilities=BackendCapabilities(text_chat=True),
                )
            )
            await context.stop_event.wait()

    host = BackendThreadHost(
        bridge,
        runtime_factory=Runtime,
        auto_restart_limit=1,
        restart_delay_ms=0,
    )
    assert host.start()
    observed: list[BackendStateEvent] = []

    def restarted_ready() -> bool:
        observed.extend(_states(bridge))
        return any(
            event.generation == 2 and event.state is BackendState.ready for event in observed
        )

    assert _pump_until(qapp, restarted_ready)
    assert host.generation == 2
    assert "PRIVATE_BACKEND_EXCEPTION_BODY" not in repr(observed)
    assert any(
        event.state is BackendState.failed and event.reason_code == "backend_crashed"
        for event in observed
    )
    assert any(event.state is BackendState.restarting for event in observed)
    host.request_stop()
    assert _pump_until(qapp, lambda: not host.has_active_generation)


def test_unexpected_backend_return_fails_without_unbounded_restart(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()

    class ReturnsEarly:
        async def run(self, _context: BackendContext) -> None:
            await asyncio.sleep(0)

    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: ReturnsEarly(),
        auto_restart_limit=0,
    )
    stopped: list[bool] = []
    host.stopped.connect(lambda: stopped.append(True))
    assert host.start()
    assert _pump_until(qapp, lambda: bool(stopped))
    events = _states(bridge)
    assert any(event.state is BackendState.failed for event in events)
    assert not host.has_active_generation


def test_skeleton_backend_rejects_chat_without_reflecting_body(qapp: QApplication) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: SkeletonBackendRuntime(),
        auto_restart_limit=0,
    )
    assert host.start()
    assert _pump_until(
        qapp,
        lambda: any(event.state is BackendState.ready for event in _states(bridge)),
    )
    command = UserMessageCommand(payload=UserMessage(text="PRIVATE_CHAT_BODY"))
    assert bridge.submit_command(command)
    observed: list[CommandRejectedEvent] = []

    def rejected() -> bool:
        observed.extend(
            event for event in bridge.drain_events() if isinstance(event, CommandRejectedEvent)
        )
        return bool(observed)

    assert _pump_until(qapp, rejected)
    assert observed[0].command_id == command.command_id
    assert observed[0].reason_code == "feature_not_available"
    assert "PRIVATE_CHAT_BODY" not in repr(observed)
    host.request_stop()
    assert _pump_until(qapp, lambda: not host.has_active_generation)


def test_backend_host_rejects_unbounded_restart_configuration(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()
    for limit, delay in ((-1, 0), (4, 0), (0, -1), (0, 5_001)):
        try:
            BackendThreadHost(
                bridge,
                auto_restart_limit=limit,
                restart_delay_ms=delay,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid restart configuration was accepted")
    qapp.processEvents()


def test_backend_context_polling_paths_are_bounded(qapp: QApplication) -> None:
    bridge = ApplicationBridge()

    async def exercise() -> None:
        stop_event = asyncio.Event()
        context = BackendContext(bridge=bridge, generation=1, stop_event=stop_event)
        command = UserMessageCommand(payload=UserMessage(text="queued"))
        bridge.submit_command(command)
        assert await context.next_commands() == [command]

        waiting = asyncio.create_task(context.next_commands(poll_seconds=0.001))
        await asyncio.sleep(0.005)
        stop_event.set()
        assert await waiting == []

    asyncio.run(exercise())
    qapp.processEvents()


def test_worker_direct_paths_cover_pre_stop_runtime_crash_and_cancellation(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()

    class WaitForStop:
        async def run(self, context: BackendContext) -> None:
            await context.stop_event.wait()

    pre_stopped = backend_module._BackendWorker(
        lambda _generation: WaitForStop(),
        bridge,
        1,
    )
    pre_stopped.request_stop()
    pre_stopped.run()
    assert not pre_stopped.crashed

    class Crash:
        async def run(self, _context: BackendContext) -> None:
            raise RuntimeError("PRIVATE_DIRECT_CRASH")

    crashed = backend_module._BackendWorker(lambda _generation: Crash(), bridge, 2)
    crashed.run()
    assert crashed.crashed

    class Exits:
        async def run(self, _context: BackendContext) -> None:
            raise SystemExit("PRIVATE_SYSTEM_EXIT")

    exited = backend_module._BackendWorker(lambda _generation: Exits(), bridge, 5)
    exited.run()
    assert exited.crashed

    class Cancel:
        def __init__(self, worker: backend_module._BackendWorker, *, request_stop: bool) -> None:
            self._worker = worker
            self._request_stop = request_stop

        async def run(self, _context: BackendContext) -> None:
            if self._request_stop:
                self._worker.request_stop()
            raise asyncio.CancelledError

    holder: list[backend_module._BackendWorker] = []
    cancelled_after_stop = backend_module._BackendWorker(
        lambda _generation: Cancel(holder[0], request_stop=True),
        bridge,
        3,
    )
    holder.append(cancelled_after_stop)
    cancelled_after_stop.run()
    assert not cancelled_after_stop.crashed

    holder.clear()
    unexpected_cancel = backend_module._BackendWorker(
        lambda _generation: Cancel(holder[0], request_stop=False),
        bridge,
        4,
    )
    holder.append(unexpected_cancel)
    unexpected_cancel.run()
    assert unexpected_cancel.crashed
    events = _states(bridge)
    assert "PRIVATE_DIRECT_CRASH" not in repr(events)
    assert "PRIVATE_SYSTEM_EXIT" not in repr(events)
    assert sum(event.state is BackendState.failed for event in events) == 3
    qapp.processEvents()
