"""Feature-gated proactive scoring and atomic user-priority coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import Lock
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
        self._scheduler_runner: ProactiveRunner | None = None
        self._scheduler_interval = _DEFAULT_SCHEDULER_INTERVAL
        self._feature_transition_lock = asyncio.Lock()
        self._feature_transition_tasks: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._latest_perception: PerceptionContext | None = None
        self._perception_epoch_lock = Lock()
        self._highest_perception_generation = -1
        self._retired_perception_generation = -1
        self._vision_accepting_perception = self._feature_enabled(FeatureName.vision)
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
        if self._scheduler_runner is not None:
            return False
        self._scheduler_runner = runner
        self._scheduler_interval = interval
        if self._feature_enabled(FeatureName.proactive):
            self._ensure_scheduler(loop)
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
        """Evaluate a trigger using only perception published through the trusted updater.

        ``perception`` remains in the signature for compatibility, but is intentionally
        ignored. Callers cannot inject or advance the trusted visual generation.
        """

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
                    perception=self._current_perception(),
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

    async def update_perception(self, perception: PerceptionContext | None) -> None:
        """Publish a sanitized observation and synchronously enforce privacy revocation."""

        self._bind_loop()
        async with self._state_lock:
            active = self._lifecycle.snapshot()
            if perception is None:
                self._latest_perception = None
                must_cancel = active.active_trigger_type == ProactiveTriggerType.visual_change.value
            else:
                must_cancel = perception.sensitive
                if self._feature_enabled(FeatureName.vision):
                    self._accept_perception(perception)
            if must_cancel:
                await self._lifecycle.cancel_active()

    async def apply_feature_state(self, state: FeatureState) -> None:
        """Waitable privacy barrier used by the feature PATCH runtime."""

        self._bind_loop()
        if state.name is FeatureName.vision:
            self._set_vision_epoch_state(state.desired_enabled)
        await self._apply_feature_transition(state)

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
        if state.name is FeatureName.vision:
            self._set_vision_epoch_state(state.desired_enabled)
        if state.name not in {FeatureName.proactive, FeatureName.vision} or self._closing:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_feature_transition, state)
        except RuntimeError:
            return

    def _schedule_feature_transition(self, state: FeatureState) -> None:
        if self._closing:
            return
        task = asyncio.create_task(
            self._apply_feature_transition(state),
            name=(
                f"proactive-feature-{state.name.value}-"
                f"{'enable' if state.desired_enabled else 'disable'}"
            ),
        )
        self._feature_transition_tasks.add(task)
        task.add_done_callback(self._feature_transition_finished)

    async def _apply_feature_transition(self, state: FeatureState) -> None:
        async with self._feature_transition_lock:
            if state.name is FeatureName.proactive:
                if state.desired_enabled:
                    if not self._closing:
                        self._ensure_scheduler()
                    return
                await self._stop_scheduler()
                async with self._state_lock:
                    await self._lifecycle.cancel_active()
                if not self._closing and self._feature_enabled(FeatureName.proactive):
                    self._ensure_scheduler()
                return
            if state.name is FeatureName.vision and not state.desired_enabled:
                self._clear_retired_perception()
                active = self._lifecycle.snapshot()
                if active.active_trigger_type == ProactiveTriggerType.visual_change.value:
                    async with self._state_lock:
                        await self._lifecycle.cancel_active()

    def _feature_transition_finished(self, task: asyncio.Task[None]) -> None:
        self._feature_transition_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    def _ensure_scheduler(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        runner = self._scheduler_runner
        if runner is None or self._closing or self._lifecycle.snapshot().closed:
            return
        task = self._scheduler_task
        if task is not None and not task.done():
            return
        active_loop = loop or self._loop
        if active_loop is None or active_loop.is_closed():
            return
        self._scheduler_stop.clear()
        task = active_loop.create_task(
            self._scheduler_loop(runner, self._scheduler_interval),
            name="proactive-idle-scheduler",
        )
        self._scheduler_task = task
        task.add_done_callback(self._scheduler_finished)

    async def _stop_scheduler(self) -> None:
        self._scheduler_stop.set()
        scheduler = self._scheduler_task
        if scheduler is None or scheduler is asyncio.current_task():
            return
        if not scheduler.done():
            scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        if self._scheduler_task is scheduler:
            self._scheduler_task = None

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
        async with self._feature_transition_lock:
            await self._stop_scheduler()
            await self._lifecycle.close()
        transitions = tuple(
            task for task in self._feature_transition_tasks if task is not asyncio.current_task()
        )
        if transitions:
            await asyncio.gather(*transitions, return_exceptions=True)

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

    def _accept_perception(self, perception: PerceptionContext) -> None:
        current = self._latest_perception
        if current is not None:
            provided_order = (perception.generation, perception.observed_at)
            current_order = (current.generation, current.observed_at)
            if provided_order < current_order:
                return
        with self._perception_epoch_lock:
            if (
                not self._vision_accepting_perception
                or perception.generation <= self._retired_perception_generation
            ):
                return
            self._highest_perception_generation = max(
                self._highest_perception_generation,
                perception.generation,
            )
            self._latest_perception = perception

    def _current_perception(self) -> PerceptionContext | None:
        current = self._latest_perception
        if current is None:
            return None
        with self._perception_epoch_lock:
            if (
                not self._vision_accepting_perception
                or current.generation <= self._retired_perception_generation
            ):
                return None
        return current

    def _set_vision_epoch_state(self, enabled: bool) -> None:
        with self._perception_epoch_lock:
            self._vision_accepting_perception = enabled
            if not enabled:
                self._retired_perception_generation = max(
                    0,
                    self._retired_perception_generation,
                    self._highest_perception_generation,
                )

    def _clear_retired_perception(self) -> None:
        current = self._latest_perception
        if current is None:
            return
        with self._perception_epoch_lock:
            if current.generation <= self._retired_perception_generation:
                self._latest_perception = None
