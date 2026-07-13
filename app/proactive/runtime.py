"""Feature-gated proactive scoring and atomic user-priority coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import FeatureFlagSource
from app.emotion import Clock, SystemClock
from app.proactive.engine import ProactiveEngine
from app.proactive.lifecycle import ProactiveLifecycle, ProactiveRunner
from app.proactive.models import (
    ProactiveContext,
    ProactiveDecision,
    ProactivePolicy,
    ProactiveSuppression,
    ProactiveTrigger,
    ProactiveTriggerType,
)
from app.schemas import FeatureName, FeatureState, PerceptionContext

_DEFAULT_SCHEDULER_INTERVAL = timedelta(seconds=30)
_IDLE_INSTRUCTION = "用户已经一段时间没有主动互动，请生成一句简短、低打扰的陪伴式问候。"


@dataclass(frozen=True, slots=True)
class ProactiveRuntimeSnapshot:
    user_turn_active: bool
    proactive_turn_active: bool
    proactive_today: int
    scheduler_active: bool
    closed: bool


class ProactiveRuntime:
    """Own cooldown/accounting state without ever converting an intent to user input."""

    def __init__(
        self,
        features: FeatureFlagSource,
        policy: ProactivePolicy | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._features = features
        self._policy = policy or ProactivePolicy()
        self._engine = ProactiveEngine(self._policy)
        self._clock = clock or SystemClock()
        self._timezone = ZoneInfo(self._policy.timezone)
        self._lifecycle = ProactiveLifecycle()
        self._state_lock = asyncio.Lock()
        self._last_user_activity = self._clock.now()
        if self._last_user_activity.tzinfo is None or self._last_user_activity.utcoffset() is None:
            raise ValueError("proactive clock 必须返回包含时区的时间")
        self._last_proactive_at: datetime | None = None
        self._daily_date: date | None = None
        self._proactive_today = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduler_stop = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._disable_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._unsubscribe: Callable[[], None] | None = self._features.subscribe(
            self._on_feature_changed
        )

    def snapshot(self) -> ProactiveRuntimeSnapshot:
        lifecycle = self._lifecycle.snapshot()
        scheduler = self._scheduler_task
        return ProactiveRuntimeSnapshot(
            user_turn_active=lifecycle.user_turn_active,
            proactive_turn_active=lifecycle.proactive_turn_active,
            proactive_today=self._proactive_today,
            scheduler_active=scheduler is not None and not scheduler.done(),
            closed=lifecycle.closed,
        )

    def start(
        self,
        runner: ProactiveRunner,
        *,
        interval: timedelta = _DEFAULT_SCHEDULER_INTERVAL,
    ) -> bool:
        """Start the production idle scheduler; repeated starts are idempotent."""

        loop = self._bind_loop()
        if interval <= timedelta(0):
            raise ValueError("proactive scheduler interval 必须大于 0")
        if self._closing or self._lifecycle.snapshot().closed:
            raise RuntimeError("ProactiveRuntime 已关闭")
        task = self._scheduler_task
        if task is not None and not task.done():
            return False
        self._scheduler_stop.clear()
        task = loop.create_task(
            self._scheduler_loop(runner, interval),
            name="proactive-idle-scheduler",
        )
        self._scheduler_task = task
        task.add_done_callback(self._scheduler_finished)
        return True

    async def submit(
        self,
        trigger: ProactiveTrigger,
        runner: ProactiveRunner,
        *,
        focus_mode: bool = False,
        do_not_disturb: bool = False,
        sensitive: bool = False,
        perception: PerceptionContext | None = None,
    ) -> ProactiveDecision:
        self._bind_loop()
        async with self._state_lock:
            now = self._clock.now()
            self._roll_daily_counter(now.astimezone(self._timezone).date())
            lifecycle = self._lifecycle.snapshot()
            decision = self._engine.evaluate(
                trigger,
                ProactiveContext(
                    now=now,
                    enabled=(
                        self._feature_enabled(FeatureName.proactive)
                        and not self._closing
                        and not lifecycle.closed
                    ),
                    user_turn_active=lifecycle.user_turn_active,
                    focus_mode=focus_mode,
                    sensitive=sensitive,
                    do_not_disturb=do_not_disturb,
                    vision_enabled=self._feature_enabled(FeatureName.vision),
                    last_user_activity=self._last_user_activity,
                    last_proactive_at=self._last_proactive_at,
                    proactive_today=self._proactive_today,
                    perception=perception,
                ),
            )
            if decision.intent is None:
                return decision
            started = await self._lifecycle.try_start(decision.intent, runner)
            if not started:
                current = self._lifecycle.snapshot()
                suppression = (
                    ProactiveSuppression.user_active
                    if current.user_turn_active
                    else ProactiveSuppression.proactive_active
                )
                return ProactiveDecision(score=decision.score, suppression=suppression)
            self._last_proactive_at = now
            self._proactive_today += 1
            return decision

    async def begin_user_turn(self, turn_id: str) -> None:
        self._bind_loop()
        await self._lifecycle.begin_user_turn(turn_id)
        async with self._state_lock:
            self._last_user_activity = self._clock.now()

    async def end_user_turn(self, turn_id: str) -> None:
        self._bind_loop()
        await self._lifecycle.end_user_turn(turn_id)

    async def wait_idle(self) -> None:
        self._bind_loop()
        await self._lifecycle.wait_idle()

    async def close(self) -> None:
        loop = self._bind_loop()
        task = self._close_task
        if task is None:
            self._closing = True
            task = loop.create_task(self._close_impl(), name="proactive-runtime-close")
            self._close_task = task
        await asyncio.shield(task)

    def _feature_enabled(self, name: FeatureName) -> bool:
        try:
            return self._features.get_feature(name).enabled
        except Exception:
            return False

    def _on_feature_changed(self, state: FeatureState) -> None:
        if state.name != FeatureName.proactive or state.enabled or self._closing:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_disable)
        except RuntimeError:
            return

    def _schedule_disable(self) -> None:
        if self._closing:
            return
        task = self._disable_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._lifecycle.cancel_active(),
            name="proactive-feature-disable",
        )
        self._disable_task = task
        task.add_done_callback(self._disable_finished)

    def _disable_finished(self, task: asyncio.Task[None]) -> None:
        if self._disable_task is task:
            self._disable_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _scheduler_loop(
        self,
        runner: ProactiveRunner,
        interval: timedelta,
    ) -> None:
        delay_seconds = interval.total_seconds()
        while not self._scheduler_stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(self._scheduler_stop.wait(), timeout=delay_seconds)
            if self._scheduler_stop.is_set():
                return
            if not self._feature_enabled(FeatureName.proactive):
                continue
            observed_at = self._clock.now()
            trigger = ProactiveTrigger(
                trigger_type=ProactiveTriggerType.idle,
                instruction=_IDLE_INSTRUCTION,
                confidence=1.0,
                novelty=1.0,
                observed_at=observed_at,
            )
            try:
                await self.submit(trigger, runner)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    def _scheduler_finished(self, task: asyncio.Task[None]) -> None:
        if self._scheduler_task is task:
            self._scheduler_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _close_impl(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            with suppress(Exception):
                unsubscribe()
        self._scheduler_stop.set()
        await self._lifecycle.close()
        scheduler = self._scheduler_task
        if scheduler is not None and scheduler is not asyncio.current_task():
            await asyncio.gather(scheduler, return_exceptions=True)
        disable = self._disable_task
        if disable is not None and disable is not asyncio.current_task():
            await asyncio.gather(disable, return_exceptions=True)

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("ProactiveRuntime 不能跨 event loop 使用")
        return loop

    def _roll_daily_counter(self, current: date) -> None:
        if self._daily_date != current:
            self._daily_date = current
            self._proactive_today = 0
