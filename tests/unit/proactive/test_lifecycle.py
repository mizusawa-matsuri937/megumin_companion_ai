"""Atomic explicit-user preemption and shutdown tests."""

import asyncio
from contextlib import suppress

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


def test_repeated_preemption_joins_one_cleanup_without_second_task_cancel() -> None:
    async def scenario() -> None:
        lifecycle = ProactiveLifecycle()
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleanup_finished = False

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            nonlocal cleanup_finished
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleanup_finished = True

        assert await lifecycle.try_start(intent(), runner)
        await started.wait()
        first = asyncio.create_task(lifecycle.cancel_active())
        await cleanup_started.wait()
        second = asyncio.create_task(lifecycle.begin_user_turn("replacement"))
        await asyncio.sleep(0)

        assert not first.done() and not second.done()
        cleanup_release.set()
        await asyncio.gather(first, second)

        assert cleanup_finished
        await lifecycle.end_user_turn("replacement")
        await lifecycle.close()

    asyncio.run(scenario())


def test_cancelled_preemption_caller_does_not_recancel_runner_cleanup() -> None:
    async def scenario() -> None:
        lifecycle = ProactiveLifecycle()
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleanup_finished = False

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            nonlocal cleanup_finished
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleanup_finished = True

        assert await lifecycle.try_start(intent(), runner)
        await started.wait()
        preempting = asyncio.create_task(lifecycle.begin_user_turn("cancelled-caller"))
        await cleanup_started.wait()
        preempting.cancel()
        with suppress(asyncio.CancelledError):
            await preempting

        cleanup_release.set()
        await lifecycle.wait_idle()
        assert cleanup_finished
        await lifecycle.end_user_turn("cancelled-caller")
        await lifecycle.close()

    asyncio.run(scenario())
