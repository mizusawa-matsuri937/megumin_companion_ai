"""Atomic explicit-user preemption and shutdown tests."""

import asyncio

from app.proactive import ProactiveLifecycle
from app.schemas import ProactiveIntent


def intent() -> ProactiveIntent:
    return ProactiveIntent(
        trigger_type="idle",
        instruction="问候用户",
        score=0.8,
        reason="test",
    )


def test_explicit_user_atomically_preempts_proactive_and_blocks_new_work() -> None:
    async def scenario() -> None:
        lifecycle = ProactiveLifecycle()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        assert await lifecycle.try_start(intent(), runner)
        assert not await lifecycle.try_start(intent(), runner)
        await started.wait()
        await lifecycle.begin_user_turn()
        assert cancelled.is_set()
        assert lifecycle.snapshot().user_turn_active
        assert not lifecycle.snapshot().proactive_turn_active
        assert not await lifecycle.try_start(intent(), runner)

        await lifecycle.begin_user_turn("replacement")
        await lifecycle.end_user_turn()
        assert lifecycle.snapshot().user_turn_active
        assert not await lifecycle.try_start(intent(), runner)
        await lifecycle.end_user_turn("replacement")

        async def complete(_intent: ProactiveIntent, _token: object) -> None:
            return None

        assert await lifecycle.try_start(intent(), complete)
        await lifecycle.wait_idle()
        assert not lifecycle.snapshot().proactive_turn_active
        await lifecycle.close()
        await lifecycle.close()
        assert lifecycle.snapshot().closed
        assert not await lifecycle.try_start(intent(), complete)

    asyncio.run(scenario())


def test_close_cancels_active_runner() -> None:
    async def scenario() -> None:
        lifecycle = ProactiveLifecycle()
        started = asyncio.Event()
        finished = asyncio.Event()

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        assert await lifecycle.try_start(intent(), runner)
        await started.wait()
        await lifecycle.close()
        assert finished.is_set()
        assert lifecycle.snapshot().closed

    asyncio.run(scenario())
