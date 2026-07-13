"""Feature gating, accounting, and user-preemption integration for proactive work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
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
    def __init__(
        self,
        enabled: bool = False,
        *,
        vision_enabled: bool = False,
        fail: bool = False,
    ) -> None:
        self.enabled = enabled
        self.vision_enabled = vision_enabled
        self.fail = fail
        self.listeners: set[Callable[[FeatureState], None]] = set()
        self.unsubscribe_calls = 0

    def get_feature(self, name: FeatureName) -> FeatureState:
        if self.fail:
            raise RuntimeError("feature store unavailable")
        enabled = self.vision_enabled if name is FeatureName.vision else self.enabled
        return FeatureState(name=name, enabled=enabled)

    def subscribe(self, listener: Callable[[FeatureState], None]) -> Callable[[], None]:
        self.listeners.add(listener)
        active = True

        def unsubscribe() -> None:
            nonlocal active
            if active:
                active = False
                self.unsubscribe_calls += 1
                self.listeners.discard(listener)

        return unsubscribe

    def set_feature(self, name: FeatureName, enabled: bool) -> None:
        if name is FeatureName.vision:
            self.vision_enabled = enabled
        else:
            self.enabled = enabled
        state = FeatureState(name=name, enabled=enabled)
        for listener in tuple(self.listeners):
            listener(state)


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


def idle_trigger() -> ProactiveTrigger:
    return ProactiveTrigger(
        trigger_type=ProactiveTriggerType.idle,
        instruction="低打扰地问候用户",
        confidence=1.0,
        novelty=1.0,
        observed_at=NOW,
    )


def visual_trigger() -> ProactiveTrigger:
    return ProactiveTrigger(
        trigger_type=ProactiveTriggerType.visual_change,
        instruction="根据已脱敏的视觉摘要简短回应",
        confidence=1.0,
        novelty=1.0,
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


def test_startup_idle_baseline_requires_full_idle_minimum() -> None:
    async def scenario() -> None:
        clock = FakeClock(NOW)
        runtime = ProactiveRuntime(
            Flags(True),
            policy(idle_minimum=timedelta(minutes=3)),
            clock=clock,
        )

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            return None

        assert (
            await runtime.submit(idle_trigger(), runner)
        ).suppression is ProactiveSuppression.insufficient_idle
        clock.advance(timedelta(minutes=3))
        assert (await runtime.submit(idle_trigger(), runner)).intent is not None
        await runtime.wait_idle()
        await runtime.close()

    asyncio.run(scenario())


def test_visual_trigger_requires_enabled_vision_and_explicit_safe_context() -> None:
    async def scenario() -> None:
        flags = Flags(True)
        runtime = ProactiveRuntime(flags, policy(), clock=FakeClock(NOW))
        safe = PerceptionContext(summary="已脱敏的普通编辑器画面", sensitive=False)
        blocked = ProactiveSuppression.visual_context_unavailable

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            return None

        assert (
            await runtime.submit(visual_trigger(), runner, perception=safe)
        ).suppression is blocked
        flags.set_feature(FeatureName.vision, True)
        assert (await runtime.submit(visual_trigger(), runner)).suppression is blocked
        sensitive = PerceptionContext(summary="敏感画面已拦截", sensitive=True)
        assert (
            await runtime.submit(visual_trigger(), runner, perception=sensitive)
        ).suppression is ProactiveSuppression.sensitive
        assert (await runtime.submit(visual_trigger(), runner, perception=safe)).intent is not None
        await runtime.wait_idle()
        await runtime.close()

    asyncio.run(scenario())


def test_disabling_feature_from_worker_thread_cancels_active_and_unsubscribes() -> None:
    async def scenario() -> None:
        flags = Flags(True)
        runtime = ProactiveRuntime(flags, policy(), clock=FakeClock(NOW))
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
        assert (
            await runtime.submit(trigger(), runner)
        ).suppression is ProactiveSuppression.proactive_active
        await asyncio.to_thread(flags.set_feature, FeatureName.proactive, False)
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await runtime.wait_idle()
        assert not runtime.snapshot().proactive_turn_active

        flags.set_feature(FeatureName.proactive, True)

        async def complete(_intent: ProactiveIntent, _token: object) -> None:
            return None

        assert (await runtime.submit(trigger(), complete)).intent is not None
        await runtime.wait_idle()
        await runtime.close()
        assert flags.unsubscribe_calls == 1
        assert flags.listeners == set()

    asyncio.run(scenario())


def test_concurrent_close_waits_for_one_cleanup_and_stops_scheduler() -> None:
    async def scenario() -> None:
        flags = Flags(True)
        runtime = ProactiveRuntime(flags, policy(), clock=FakeClock(NOW))
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def runner(_intent: ProactiveIntent, _token: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await release_cleanup.wait()

        assert runtime.start(runner, interval=timedelta(hours=1))
        assert runtime.snapshot().scheduler_active
        assert (await runtime.submit(trigger(), runner)).intent is not None
        await started.wait()

        first = asyncio.create_task(runtime.close())
        await cleanup_started.wait()
        second = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()

        release_cleanup.set()
        await asyncio.gather(first, second)
        assert runtime.snapshot().closed
        assert not runtime.snapshot().scheduler_active
        assert flags.unsubscribe_calls == 1

    asyncio.run(scenario())


def test_scheduler_is_idempotent_and_never_outputs_while_feature_is_off() -> None:
    async def scenario() -> None:
        flags = Flags()
        clock = FakeClock(NOW)
        runtime = ProactiveRuntime(
            flags,
            policy(
                idle_minimum=timedelta(minutes=3),
                cooldown=timedelta(hours=1),
            ),
            clock=clock,
        )
        called = asyncio.Event()
        trigger_types: list[str] = []

        async def runner(intent: ProactiveIntent, _token: object) -> None:
            trigger_types.append(intent.trigger_type)
            called.set()

        interval = timedelta(milliseconds=2)
        with pytest.raises(ValueError, match="interval"):
            runtime.start(runner, interval=timedelta(0))
        assert runtime.start(runner, interval=interval)
        assert not runtime.start(runner, interval=interval)
        await asyncio.sleep(0.02)
        assert not called.is_set()

        clock.advance(timedelta(minutes=3))
        flags.set_feature(FeatureName.proactive, True)
        await asyncio.wait_for(called.wait(), timeout=1)
        assert trigger_types == [ProactiveTriggerType.idle.value]

        await runtime.close()
        with pytest.raises(RuntimeError, match="已关闭"):
            runtime.start(runner, interval=interval)

    asyncio.run(scenario())
