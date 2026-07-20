"""QThread-owned asyncio backend lifecycle for the W13 desktop spike."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    BackendState,
    BackendStateEvent,
    BridgeCommand,
    CommandRejectedEvent,
)


@dataclass(frozen=True, slots=True)
class BackendContext:
    bridge: ApplicationBridge
    generation: int
    stop_event: asyncio.Event

    async def next_commands(self, *, poll_seconds: float = 0.005) -> list[BridgeCommand]:
        """Poll a bounded shared queue without creating an uncancellable executor thread."""

        while not self.stop_event.is_set():
            commands = self.bridge.take_commands()
            if commands:
                return list(commands)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue
        return []


class BackendRuntime(Protocol):
    async def run(self, context: BackendContext) -> None: ...


BackendRuntimeFactory = Callable[[int], BackendRuntime]
MAX_RETIRED_THREADS = 4


class SkeletonBackendRuntime:
    """Honest W13 runtime: lifecycle is real; chat composition remains W14 work."""

    async def run(self, context: BackendContext) -> None:
        context.bridge.publish_event(
            BackendStateEvent(
                generation=context.generation,
                state=BackendState.ready,
                capabilities=BackendCapabilities(),
            )
        )
        while not context.stop_event.is_set():
            commands = await context.next_commands()
            for command in commands:
                context.bridge.publish_event(
                    CommandRejectedEvent(
                        command_id=command.command_id,
                        reason_code="feature_not_available",
                    )
                )


class _BackendWorker(QThread):
    def __init__(
        self,
        runtime_factory: BackendRuntimeFactory,
        bridge: ApplicationBridge,
        generation: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"BackendThread-{generation}")
        self._runtime_factory = runtime_factory
        self._bridge = bridge
        self.generation = generation
        self.crashed = False
        self._state_lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = False

    def request_stop(self) -> None:
        with self._state_lock:
            self._stop_requested = True
            loop = self._loop
            stop_event = self._stop_event
        if loop is not None and stop_event is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(stop_event.set)

    def run(self) -> None:
        self._bridge.publish_event(
            BackendStateEvent(generation=self.generation, state=BackendState.starting)
        )
        try:
            asyncio.run(self._run_runtime())
        except asyncio.CancelledError:
            with self._state_lock:
                stop_requested = self._stop_requested
            if stop_requested:
                self._publish_stopped()
            else:
                self._publish_failed()
        except BaseException:
            self._publish_failed()
        else:
            self._publish_stopped()
        finally:
            with self._state_lock:
                self._loop = None
                self._stop_event = None

    async def _run_runtime(self) -> None:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        with self._state_lock:
            self._loop = loop
            self._stop_event = stop_event
            stop_requested = self._stop_requested
        if stop_requested:
            stop_event.set()
        runtime = self._runtime_factory(self.generation)
        await runtime.run(
            BackendContext(
                bridge=self._bridge,
                generation=self.generation,
                stop_event=stop_event,
            )
        )
        if not stop_event.is_set():
            raise RuntimeError("backend runtime exited without a stop request")

    def _publish_failed(self) -> None:
        self.crashed = True
        self._bridge.publish_event(
            BackendStateEvent(
                generation=self.generation,
                state=BackendState.failed,
                reason_code="backend_crashed",
            )
        )

    def _publish_stopped(self) -> None:
        self._bridge.publish_event(
            BackendStateEvent(generation=self.generation, state=BackendState.stopped)
        )


class BackendThreadHost(QObject):
    """Own one backend generation and at most a bounded number of crash restarts."""

    generation_started = Signal(int)
    generation_finished = Signal(int, bool)
    stopped = Signal()

    def __init__(
        self,
        bridge: ApplicationBridge,
        *,
        runtime_factory: BackendRuntimeFactory | None = None,
        auto_restart_limit: int = 1,
        restart_delay_ms: int = 25,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not 0 <= auto_restart_limit <= 3:
            raise ValueError("auto restart limit must be between zero and three")
        if not 0 <= restart_delay_ms <= 5_000:
            raise ValueError("restart delay is outside the W13 bound")
        self._bridge = bridge
        self._runtime_factory = runtime_factory or (lambda _generation: SkeletonBackendRuntime())
        self._auto_restart_limit = auto_restart_limit
        self._restart_delay_ms = restart_delay_ms
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._start_generation)
        self._thread: _BackendWorker | None = None
        self._retired_threads: list[_BackendWorker] = []
        self._generation = 0
        self._restart_count = 0
        self._stopping = True
        self._stopped_notified = True

    def start(self) -> bool:
        if self.has_active_generation:
            return False
        self._stopping = False
        self._stopped_notified = False
        self._restart_count = 0
        self._start_generation()
        return True

    def request_stop(self) -> None:
        self._stopping = True
        self._restart_timer.stop()
        thread = self._thread
        if thread is not None:
            self._bridge.publish_event(
                BackendStateEvent(generation=thread.generation, state=BackendState.stopping)
            )
            thread.request_stop()
        else:
            self._emit_stopped_once()

    def wait_for_current_thread(self, timeout_ms: int) -> bool:
        thread = self._thread
        return thread is None or thread.wait(timeout_ms)

    @property
    def has_active_generation(self) -> bool:
        return self._thread is not None or self._restart_timer.isActive()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def retired_thread_count(self) -> int:
        return len(self._retired_threads)

    def _start_generation(self) -> None:
        if self._stopping or self._thread is not None:
            return
        self._generation += 1
        thread = _BackendWorker(
            self._runtime_factory,
            self._bridge,
            self._generation,
            self,
        )
        self._thread = thread
        thread.finished.connect(self._on_thread_finished)
        self.generation_started.emit(self._generation)
        thread.start()

    def _on_thread_finished(self) -> None:
        sender = self.sender()
        if not isinstance(sender, _BackendWorker):
            return
        thread = sender
        if self._thread is not thread:
            return
        thread.wait()
        crashed = thread.crashed
        generation = thread.generation
        self._thread = None
        thread.setParent(None)
        self._retired_threads.append(thread)
        if len(self._retired_threads) > MAX_RETIRED_THREADS:
            del self._retired_threads[0]
        self.generation_finished.emit(generation, crashed)
        if crashed and not self._stopping and self._restart_count < self._auto_restart_limit:
            self._restart_count += 1
            self._bridge.publish_event(
                BackendStateEvent(
                    generation=generation,
                    state=BackendState.restarting,
                    reason_code="backend_restart_scheduled",
                )
            )
            self._restart_timer.start(self._restart_delay_ms)
            return
        self._emit_stopped_once()

    def _emit_stopped_once(self) -> None:
        if not self._stopped_notified:
            self._stopped_notified = True
            self.stopped.emit()
