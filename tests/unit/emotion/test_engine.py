from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from app.emotion.clock import FakeClock
from app.emotion.engine import EmotionEngine
from app.emotion.models import (
    EmotionDimension,
    EmotionLabel,
    EmotionState,
    EmotionStimulus,
    EmotionSuggestion,
    StimulusKind,
)
from hypothesis import given
from hypothesis import strategies as st


def _time() -> datetime:
    return datetime(2026, 7, 13, 12, tzinfo=UTC)


def _stimulus(clock: FakeClock, index: int, kind: StimulusKind) -> EmotionStimulus:
    return EmotionStimulus(
        stimulus_id=f"stim-{index}",
        kind=kind,
        occurred_at=clock.now(),
        reason_code=f"test_{kind.value}",
    )


def test_rule_application_is_bounded_and_explainable() -> None:
    clock = FakeClock(_time())
    engine = EmotionEngine(clock=clock)

    transition = engine.apply(_stimulus(clock, 1, StimulusKind.user_distress))

    assert transition.requested_delta[EmotionDimension.concern] == pytest.approx(0.15)
    assert transition.applied_delta[EmotionDimension.concern] == pytest.approx(0.15)
    assert transition.after.concern == pytest.approx(0.30)
    assert transition.after.reason_code == "test_user_distress"
    assert transition.before.concern == pytest.approx(0.15)


def test_event_and_rolling_minute_limits_are_enforced() -> None:
    clock = FakeClock(_time())
    initial = EmotionState(
        concern=0.20,
        last_updated_at=clock.now(),
        label_since=clock.now(),
    )
    engine = EmotionEngine(clock=clock, initial=initial)

    first = engine.apply(_stimulus(clock, 1, StimulusKind.user_distress))
    second = engine.apply(_stimulus(clock, 2, StimulusKind.user_distress))
    third = engine.apply(_stimulus(clock, 3, StimulusKind.late_night))

    assert first.applied_delta[EmotionDimension.concern] == pytest.approx(0.15)
    assert second.applied_delta[EmotionDimension.concern] == pytest.approx(0.10)
    assert third.applied_delta[EmotionDimension.concern] == pytest.approx(0.0)
    assert EmotionDimension.concern in second.limited_dimensions
    assert engine.state.concern == pytest.approx(0.45)

    clock.advance(timedelta(seconds=61))
    fourth = engine.apply(_stimulus(clock, 4, StimulusKind.late_night))
    assert fourth.applied_delta[EmotionDimension.concern] == pytest.approx(0.12)


def test_rolling_limit_counts_changes_in_both_directions() -> None:
    clock = FakeClock(_time())
    engine = EmotionEngine(clock=clock)

    raised = engine.apply(_stimulus(clock, 1, StimulusKind.inactivity))
    lowered = engine.apply(_stimulus(clock, 2, StimulusKind.neutral_interaction))
    raised_again = engine.apply(_stimulus(clock, 3, StimulusKind.inactivity))
    final = engine.apply(_stimulus(clock, 4, StimulusKind.inactivity))

    assert raised.applied_delta[EmotionDimension.boredom] == pytest.approx(0.08)
    assert lowered.applied_delta[EmotionDimension.boredom] == pytest.approx(-0.03)
    assert raised_again.applied_delta[EmotionDimension.boredom] == pytest.approx(0.08)
    assert final.applied_delta[EmotionDimension.boredom] == pytest.approx(0.06)


def test_values_clamp_to_unit_interval() -> None:
    clock = FakeClock(_time())
    initial = EmotionState(
        concern=0.98,
        last_updated_at=clock.now(),
        label_since=clock.now(),
    )
    engine = EmotionEngine(clock=clock, initial=initial)

    transition = engine.apply(_stimulus(clock, 1, StimulusKind.user_distress))

    assert transition.after.concern == 1.0
    assert transition.applied_delta[EmotionDimension.concern] == pytest.approx(0.02)
    assert EmotionDimension.concern in transition.limited_dimensions


def test_decay_uses_fake_clock_without_sleeping() -> None:
    clock = FakeClock(_time())
    initial = EmotionState(
        concern=0.75,
        embarrassment=0.70,
        last_updated_at=clock.now(),
        label_since=clock.now(),
    )
    engine = EmotionEngine(clock=clock, initial=initial)
    clock.advance(timedelta(minutes=2))

    transition = engine.decay()

    assert transition.kind is StimulusKind.time_decay
    assert transition.after.concern < initial.concern
    assert transition.after.concern > 0.15
    assert transition.after.embarrassment < initial.embarrassment
    assert transition.after.last_updated_at == clock.now()


def test_dominant_label_has_stability_period() -> None:
    clock = FakeClock(_time())
    initial = EmotionState(
        pride=0.70,
        dominant_label=EmotionLabel.proud,
        last_updated_at=clock.now(),
        label_since=clock.now(),
    )
    engine = EmotionEngine(clock=clock, initial=initial)

    focused = engine.apply(_stimulus(clock, 1, StimulusKind.focused_activity))
    assert focused.after.dominant_label is EmotionLabel.proud
    assert focused.label_change_suppressed

    clock.advance(timedelta(seconds=15))
    focused = engine.apply(_stimulus(clock, 2, StimulusKind.focused_activity))
    assert focused.after.dominant_label is EmotionLabel.focused
    assert focused.label_changed


def test_out_of_order_stimulus_is_rejected() -> None:
    clock = FakeClock(_time())
    engine = EmotionEngine(clock=clock)
    clock.advance(timedelta(seconds=1))
    engine.apply(_stimulus(clock, 1, StimulusKind.praise))

    stale = EmotionStimulus(
        stimulus_id="stale",
        kind=StimulusKind.praise,
        occurred_at=_time(),
        reason_code="stale",
    )
    with pytest.raises(ValueError, match="chronological"):
        engine.apply(stale)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EmotionEngine(max_delta_per_event=0.0),
        lambda: EmotionEngine(max_delta_per_minute=1.1),
        lambda: EmotionEngine(label_min_duration=timedelta(seconds=-1)),
    ],
)
def test_engine_configuration_limits_are_validated(
    factory: Callable[[], EmotionEngine],
) -> None:
    with pytest.raises(ValueError):
        factory()


def test_decay_and_stimulus_time_guards() -> None:
    clock = FakeClock(_time())
    engine = EmotionEngine(clock=clock)
    decay = engine.decay()
    assert decay.applied_delta == {}
    with pytest.raises(ValueError, match="use decay"):
        engine.apply(_stimulus(clock, 1, StimulusKind.time_decay))
    with pytest.raises(ValueError, match="backwards"):
        engine.decay(at=_time() - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"energy": 0.10}, EmotionLabel.sleepy),
        ({"energy": 0.70, "explosion_urge": 0.90}, EmotionLabel.explosion_mode),
        ({"concern": 0.70}, EmotionLabel.worried),
        ({"embarrassment": 0.70}, EmotionLabel.shy),
        ({"boredom": 0.80}, EmotionLabel.bored),
        ({"curiosity": 0.70, "energy": 0.60}, EmotionLabel.excited),
        ({"pride": 0.70}, EmotionLabel.proud),
        ({"affection": 0.70}, EmotionLabel.happy),
    ],
)
def test_all_dominant_label_rules(overrides: dict[str, float], expected: EmotionLabel) -> None:
    clock = FakeClock(_time())
    initial = EmotionState.model_validate(
        {
            **overrides,
            "last_updated_at": clock.now(),
            "label_since": clock.now(),
        }
    )
    engine = EmotionEngine(clock=clock, initial=initial)

    transition = engine.apply(_stimulus(clock, 1, StimulusKind.neutral_interaction))

    assert transition.after.dominant_label is expected


def test_explosion_mode_cannot_reenter_during_cooldown() -> None:
    clock = FakeClock(_time())
    initial = EmotionState(
        energy=0.70,
        explosion_urge=0.83,
        last_updated_at=clock.now(),
        label_since=clock.now(),
    )
    engine = EmotionEngine(clock=clock, initial=initial)
    entered = engine.apply(_stimulus(clock, 1, StimulusKind.neutral_interaction))
    assert entered.after.dominant_label is EmotionLabel.explosion_mode

    clock.advance(timedelta(minutes=1))
    left = engine.decay()
    assert left.after.dominant_label is EmotionLabel.neutral
    reentry = engine.apply(_stimulus(clock, 2, StimulusKind.explosion_topic))
    assert reentry.after.dominant_label is EmotionLabel.neutral
    assert reentry.label_change_suppressed


@given(
    confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    intensity=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    kind=st.sampled_from([kind for kind in StimulusKind if kind is not StimulusKind.time_decay]),
)
def test_llm_suggestions_remain_bounded_deterministic_inputs(
    confidence: float,
    intensity: float,
    kind: StimulusKind,
) -> None:
    clock = FakeClock(_time())
    engine = EmotionEngine(clock=clock)
    transition = engine.apply_suggestion(
        EmotionSuggestion(
            suggestion_id="llm-hint",
            kind=kind,
            confidence=confidence,
            intensity=intensity,
            occurred_at=clock.now(),
        )
    )

    assert all(0.0 <= transition.after.value(dimension) <= 1.0 for dimension in EmotionDimension)
    assert all(abs(delta) <= 0.15 for delta in transition.applied_delta.values())
    assert transition.reason_code == f"llm_suggestion_{kind.value}"


def test_llm_suggestion_cannot_invoke_decay() -> None:
    clock = FakeClock(_time())
    suggestion = EmotionSuggestion(
        suggestion_id="invalid",
        kind=StimulusKind.time_decay,
        confidence=1.0,
        occurred_at=clock.now(),
    )
    with pytest.raises(ValueError, match="cannot request"):
        EmotionEngine(clock=clock).apply_suggestion(suggestion)
