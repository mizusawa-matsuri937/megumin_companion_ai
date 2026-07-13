"""Scoring, timezone, cooldown, and privacy suppression tests."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from app.proactive import (
    ProactiveContext,
    ProactiveEngine,
    ProactivePolicy,
    ProactiveSuppression,
    ProactiveTrigger,
    ProactiveTriggerType,
)
from app.schemas import PerceptionContext
from hypothesis import given
from hypothesis import strategies as st

NOW = datetime(2026, 7, 13, 4, 0, tzinfo=UTC)  # 12:00 Asia/Shanghai


def trigger(**changes: object) -> ProactiveTrigger:
    values: dict[str, object] = {
        "trigger_type": ProactiveTriggerType.task_complete,
        "instruction": "庆祝刚完成的任务",
        "confidence": 0.9,
        "novelty": 0.8,
        "urgency": 0.2,
        "observed_at": NOW,
    }
    values.update(changes)
    return ProactiveTrigger(**values)  # type: ignore[arg-type]


def context(**changes: object) -> ProactiveContext:
    values: dict[str, object] = {"now": NOW, "enabled": True}
    values.update(changes)
    return ProactiveContext(**values)  # type: ignore[arg-type]


@given(
    confidence=st.floats(min_value=0, max_value=1, allow_nan=False),
    novelty=st.floats(min_value=0, max_value=1, allow_nan=False),
    urgency=st.floats(min_value=0, max_value=1, allow_nan=False),
    trigger_type=st.sampled_from(list(ProactiveTriggerType)),
)
def test_score_is_always_clamped(
    confidence: float,
    novelty: float,
    urgency: float,
    trigger_type: ProactiveTriggerType,
) -> None:
    score = ProactiveEngine().score(
        trigger(
            confidence=confidence,
            novelty=novelty,
            urgency=urgency,
            trigger_type=trigger_type,
        )
    )
    assert 0.0 <= score <= 1.0


def test_allowed_decision_contains_only_internal_intent() -> None:
    decision = ProactiveEngine().evaluate(trigger(), context())

    assert decision.suppression is ProactiveSuppression.allowed
    assert decision.intent is not None
    assert decision.intent.instruction == "用户刚完成一项任务；生成一句简短、克制的祝贺。"
    assert decision.intent.reason == "deterministic_policy_allowed"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"enabled": False}, ProactiveSuppression.disabled),
        ({"user_turn_active": True}, ProactiveSuppression.user_active),
        ({"focus_mode": True}, ProactiveSuppression.focus_mode),
        ({"sensitive": True}, ProactiveSuppression.sensitive),
        ({"do_not_disturb": True}, ProactiveSuppression.do_not_disturb),
        ({"proactive_today": 8}, ProactiveSuppression.daily_limit),
        (
            {"last_proactive_at": NOW - timedelta(minutes=1)},
            ProactiveSuppression.cooldown,
        ),
    ],
)
def test_suppression_precedence(changes: dict[str, object], expected: ProactiveSuppression) -> None:
    decision = ProactiveEngine().evaluate(trigger(), context(**changes))
    assert decision.suppression is expected
    assert decision.intent is None


def test_sensitive_perception_and_quiet_local_time_are_suppressed() -> None:
    perception = PerceptionContext(summary="已脱敏", sensitive=True, observed_at=NOW)
    assert (
        ProactiveEngine().evaluate(trigger(), context(perception=perception)).suppression
        is ProactiveSuppression.sensitive
    )

    quiet_utc = datetime(2026, 7, 13, 16, 0, tzinfo=UTC)  # 00:00 Asia/Shanghai
    assert (
        ProactiveEngine().evaluate(trigger(), context(now=quiet_utc)).suppression
        is ProactiveSuppression.quiet_hours
    )


def test_idle_minimum_and_threshold_are_enforced() -> None:
    idle = trigger(trigger_type=ProactiveTriggerType.idle)
    too_soon = context(last_user_activity=NOW - timedelta(seconds=30))
    assert (
        ProactiveEngine().evaluate(idle, too_soon).suppression
        is ProactiveSuppression.insufficient_idle
    )

    policy = ProactivePolicy(minimum_score=1.0)
    assert (
        ProactiveEngine(policy).evaluate(trigger(), context()).suppression
        is ProactiveSuppression.below_threshold
    )


def test_visual_context_must_be_fresh_and_cannot_come_from_the_future() -> None:
    visual = trigger(trigger_type=ProactiveTriggerType.visual_change)
    for observed_at in (NOW - timedelta(seconds=31), NOW + timedelta(seconds=1)):
        decision = ProactiveEngine().evaluate(
            visual,
            context(
                vision_enabled=True,
                perception=PerceptionContext(summary="已脱敏", observed_at=observed_at),
            ),
        )
        assert decision.suppression is ProactiveSuppression.visual_context_unavailable


def test_invalid_timezone_and_policy_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ProactiveEngine(ProactivePolicy(timezone="Mars/Olympus"))
    with pytest.raises(ValueError):
        ProactivePolicy(daily_limit=0)


def test_policy_copies_and_freezes_custom_trigger_weights() -> None:
    weights = dict(ProactivePolicy().trigger_weights)
    policy = ProactivePolicy(trigger_weights=weights)
    original = policy.trigger_weights[ProactiveTriggerType.idle]

    weights[ProactiveTriggerType.idle] = 1.0
    assert policy.trigger_weights[ProactiveTriggerType.idle] == original
    with pytest.raises(TypeError):
        cast(dict[ProactiveTriggerType, float], policy.trigger_weights)[
            ProactiveTriggerType.idle
        ] = 1.0
