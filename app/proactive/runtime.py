"""Feature-gated proactive scoring and atomic user-priority coordination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
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
)
from app.schemas import FeatureName, PerceptionContext


@dataclass(frozen=True, slots=True)
class ProactiveRuntimeSnapshot:
    user_turn_active: bool
    proactive_turn_active: bool
    proactive_today: int
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
        self._last_user_activity: datetime | None = None
        self._last_proactive_at: datetime | None = None
        self._daily_date: date | None = None
        self._proactive_today = 0

    def snapshot(self) -> ProactiveRuntimeSnapshot:
        lifecycle = self._lifecycle.snapshot()
        return ProactiveRuntimeSnapshot(
            user_turn_active=lifecycle.user_turn_active,
            proactive_turn_active=lifecycle.proactive_turn_active,
            proactive_today=self._proactive_today,
            closed=lifecycle.closed,
        )

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
        async with self._state_lock:
            now = self._clock.now()
            self._roll_daily_counter(now.astimezone(self._timezone).date())
            lifecycle = self._lifecycle.snapshot()
            decision = self._engine.evaluate(
                trigger,
                ProactiveContext(
                    now=now,
                    enabled=self._is_enabled() and not lifecycle.closed,
                    user_turn_active=lifecycle.user_turn_active,
                    focus_mode=focus_mode,
                    sensitive=sensitive,
                    do_not_disturb=do_not_disturb,
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
        await self._lifecycle.begin_user_turn(turn_id)
        async with self._state_lock:
            self._last_user_activity = self._clock.now()

    async def end_user_turn(self, turn_id: str) -> None:
        await self._lifecycle.end_user_turn(turn_id)

    async def wait_idle(self) -> None:
        await self._lifecycle.wait_idle()

    async def close(self) -> None:
        await self._lifecycle.close()

    def _is_enabled(self) -> bool:
        try:
            return self._features.get_feature(FeatureName.proactive).enabled
        except Exception:
            return False

    def _roll_daily_counter(self, current: date) -> None:
        if self._daily_date != current:
            self._daily_date = current
            self._proactive_today = 0
