"""Atomic user-priority arbitration for proactive turns."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core import CancellationToken
from app.schemas import ProactiveIntent

ProactiveRunner = Callable[[ProactiveIntent, CancellationToken], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ProactiveLifecycleSnapshot:
    user_turn_active: bool
    proactive_turn_active: bool
    closed: bool


class ProactiveLifecycle:
    """Guarantee that proactive work never starts over, or survives, explicit input."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._user_turns: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._token: CancellationToken | None = None
        self._cancellation_started = False
        self._closed = False

    def snapshot(self) -> ProactiveLifecycleSnapshot:
        task = self._task
        return ProactiveLifecycleSnapshot(
            user_turn_active=bool(self._user_turns),
            proactive_turn_active=task is not None and not task.done(),
            closed=self._closed,
        )

    async def try_start(self, intent: ProactiveIntent, runner: ProactiveRunner) -> bool:
        async with self._lock:
            if self._closed or self._user_turns:
                return False
            if self._task is not None and not self._task.done():
                return False
            token = CancellationToken(intent.intent_id)
            task = asyncio.create_task(
                self._run(intent, token, runner),
                name=f"proactive-{intent.intent_id}",
            )
            self._token = token
            self._task = task
            self._cancellation_started = False
            return True

    async def begin_user_turn(self, turn_id: str = "user") -> None:
        async with self._lock:
            self._user_turns.add(turn_id)
            task = self._task
            self._signal_cancel_locked()
        await self._join_and_clear(task)

    async def end_user_turn(self, turn_id: str = "user") -> None:
        async with self._lock:
            self._user_turns.discard(turn_id)

    async def wait_idle(self) -> None:
        async with self._lock:
            task = self._task
        await self._join_and_clear(task)

    async def cancel_active(self) -> None:
        """Cancel current proactive work without permanently closing the lifecycle."""

        async with self._lock:
            task = self._task
            self._signal_cancel_locked()
        await self._join_and_clear(task)

    async def _run(
        self,
        intent: ProactiveIntent,
        token: CancellationToken,
        runner: ProactiveRunner,
    ) -> None:
        task = asyncio.current_task()
        try:
            await runner(intent, token)
            token.raise_if_cancelled()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Proactive work is best-effort background activity. The runner owns
            # structured error reporting; its failure must not leak an unhandled task.
            return
        finally:
            async with self._lock:
                if self._task is task:
                    self._task = None
                    self._token = None
                    self._cancellation_started = False

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                self._closed = True
            task = self._task
            self._signal_cancel_locked()
        await self._join_and_clear(task)

    async def _join_and_clear(self, task: asyncio.Task[None] | None) -> None:
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                pass
        async with self._lock:
            if self._task is task and task is not None and task.done():
                self._task = None
                self._token = None
                self._cancellation_started = False

    def _signal_cancel_locked(self) -> None:
        if self._cancellation_started:
            return
        token = self._token
        task = self._task
        if token is not None:
            token.cancel()
        if task is not None and task is not asyncio.current_task() and not task.done():
            self._cancellation_started = True
            task.cancel()
