"""Feature gating, accounting, and user-preemption integration for proactive work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.emotion import FakeClock
from app.proactive import (
    ProactivePolicy,
    ProactiveRuntime,
    ProactiveSuppression,
    ProactiveTrigger,
    ProactiveTriggerType,
)
from app.schemas import FeatureName, FeatureState, PerceptionContext, ProactiveIntent


class Flags:
    def __init__(self, enabled: bool = False, *, fail: bool = False) -> None:
        self.enabled = enabled
        self.fail = fail

    def get_feature(self, name: FeatureName) -> FeatureState:
        if self.fail:
            raise RuntimeError("feature store unavailable")
        return FeatureState(name=name, enabled=self.enabled)

    def subscribe(self, _listener: Callable[[FeatureState], None]) -> Callable[[], None]:
        return lambda: None


NOW = datetime(2026, 7, 13, 4, 0, tzinfo=UTC)


def trigger() -> ProactiveTrigger:
    return ProactiveTrigger(
        trigger_type=ProactiveTriggerType.task_complete,
        instruction="庆祝完成任务",
        confidence=1.0,
        novelty=1.0,
        urgency=1.0,
        observed_at=NOW,
    )


def policy(**changes: object) -> ProactivePolicy:
    values: dict[str, object] = {
        "minimum_score": 0.0,
        "cooldown": timedelta(0),
        "daily_limit": 2,
    }
    values.update(changes)
    return ProactivePolicy(**values)  # type: ignore[arg-type]


def test_disabled_privacy_controls_and_daily_accounting_are_deterministic() -> None:
    async def scenario() -> None:
        flags = Flags()
        clock = FakeClock(NOW)
        runtime = ProactiveRuntime(flags, policy(), clock=clock)
        calls = 0

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            nonlocal calls
            calls += 1

        assert (
            await runtime.submit(trigger(), runner)
        ).suppression is ProactiveSuppression.disabled
        flags.fail = True
        assert (
            await runtime.submit(trigger(), runner)
        ).suppression is ProactiveSuppression.disabled
        flags.fail = False
        flags.enabled = True
        assert (
            await runtime.submit(trigger(), runner, focus_mode=True)
        ).suppression is ProactiveSuppression.focus_mode
        assert (
            await runtime.submit(
                trigger(),
                runner,
                perception=PerceptionContext(summary="已脱敏", sensitive=True),
            )
        ).suppression is ProactiveSuppression.sensitive

        assert (await runtime.submit(trigger(), runner)).intent is not None
        await runtime.wait_idle()
        assert (await runtime.submit(trigger(), runner)).intent is not None
        await runtime.wait_idle()
        assert runtime.snapshot().proactive_today == 2
        assert (
            await runtime.submit(trigger(), runner)
        ).suppression is ProactiveSuppression.daily_limit

        clock.advance(timedelta(days=1))
        assert (await runtime.submit(trigger(), runner)).intent is not None
        await runtime.wait_idle()
        assert runtime.snapshot().proactive_today == 1
        assert calls == 3
        await runtime.close()
        assert (
            await runtime.submit(trigger(), runner)
        ).suppression is ProactiveSuppression.disabled

    asyncio.run(scenario())


def test_explicit_user_preempts_background_runner_and_multiple_markers_are_atomic() -> None:
    async def scenario() -> None:
        runtime = ProactiveRuntime(Flags(True), policy(), clock=FakeClock(NOW))
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        assert (await runtime.submit(trigger(), runner)).intent is not None
        await started.wait()
        await runtime.begin_user_turn("old")
        assert cancelled.is_set()
        await runtime.begin_user_turn("new")
        await runtime.end_user_turn("old")
        assert runtime.snapshot().user_turn_active
        assert (
            await runtime.submit(trigger(), runner)
        ).suppression is ProactiveSuppression.user_active
        await runtime.end_user_turn("new")
        assert not runtime.snapshot().user_turn_active
        await runtime.close()

    asyncio.run(scenario())


def test_background_runner_failure_is_contained() -> None:
    async def scenario() -> None:
        runtime = ProactiveRuntime(Flags(True), policy(), clock=FakeClock(NOW))

        async def failed(_intent: ProactiveIntent, _token: object) -> None:
            raise RuntimeError("synthetic failure")

        assert (await runtime.submit(trigger(), failed)).intent is not None
        await runtime.wait_idle()
        assert not runtime.snapshot().proactive_turn_active
        await runtime.close()

    asyncio.run(scenario())
