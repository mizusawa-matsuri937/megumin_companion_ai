"""Deterministic scoring with privacy and interruption suppression."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.proactive.models import (
    ProactiveContext,
    ProactiveDecision,
    ProactivePolicy,
    ProactiveSuppression,
    ProactiveTrigger,
    ProactiveTriggerType,
)
from app.schemas import ProactiveIntent


class ProactiveEngine:
    def __init__(self, policy: ProactivePolicy | None = None) -> None:
        self._policy = policy or ProactivePolicy()
        try:
            self._timezone = ZoneInfo(self._policy.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("无效 proactive timezone") from exc

    def score(self, trigger: ProactiveTrigger) -> float:
        base = self._policy.trigger_weights[trigger.trigger_type]
        value = base * 0.45 + trigger.confidence * 0.3 + trigger.novelty * 0.15
        value += trigger.urgency * 0.1
        return min(1.0, max(0.0, round(value, 6)))

    def evaluate(
        self,
        trigger: ProactiveTrigger,
        context: ProactiveContext,
    ) -> ProactiveDecision:
        score = self.score(trigger)
        suppression = self._suppression(trigger, context, score)
        if suppression is not ProactiveSuppression.allowed:
            return ProactiveDecision(score=score, suppression=suppression)
        return ProactiveDecision(
            score=score,
            suppression=ProactiveSuppression.allowed,
            intent=ProactiveIntent(
                trigger_type=trigger.trigger_type.value,
                instruction=trigger.instruction.strip(),
                score=score,
                voice_allowed=trigger.voice_allowed,
                reason="deterministic_policy_allowed",
            ),
        )

    def _suppression(
        self,
        trigger: ProactiveTrigger,
        context: ProactiveContext,
        score: float,
    ) -> ProactiveSuppression:
        if not context.enabled:
            return ProactiveSuppression.disabled
        if context.user_turn_active:
            return ProactiveSuppression.user_active
        if context.focus_mode:
            return ProactiveSuppression.focus_mode
        if context.sensitive:
            return ProactiveSuppression.sensitive
        if trigger.trigger_type is ProactiveTriggerType.visual_change:
            if not context.vision_enabled or context.perception is None:
                return ProactiveSuppression.visual_context_unavailable
            if context.perception.sensitive:
                return ProactiveSuppression.sensitive
        elif context.perception is not None and context.perception.sensitive:
            return ProactiveSuppression.sensitive
        if context.do_not_disturb:
            return ProactiveSuppression.do_not_disturb
        if self._is_quiet_hour(context):
            return ProactiveSuppression.quiet_hours
        if context.proactive_today >= self._policy.daily_limit:
            return ProactiveSuppression.daily_limit
        if context.last_proactive_at is not None:
            since_last = context.now - context.last_proactive_at
            if since_last < self._policy.cooldown:
                return ProactiveSuppression.cooldown
        if trigger.trigger_type is ProactiveTriggerType.idle:
            if context.last_user_activity is None:
                idle_for = timedelta.max
            else:
                idle_for = context.now - context.last_user_activity
            if idle_for < self._policy.idle_minimum:
                return ProactiveSuppression.insufficient_idle
        if score < self._policy.minimum_score:
            return ProactiveSuppression.below_threshold
        return ProactiveSuppression.allowed

    def _is_quiet_hour(self, context: ProactiveContext) -> bool:
        hour = context.now.astimezone(self._timezone).hour
        start = self._policy.quiet_start_hour
        end = self._policy.quiet_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end
