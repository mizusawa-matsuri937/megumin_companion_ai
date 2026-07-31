"""Whole-turn emotion presentation freezing for W28."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from app.emotion import (
    EmotionEngine,
    EmotionLabel,
    EmotionSegmentDecorator,
    ExpressionCooldown,
)
from app.emotion.clock import FakeClock
from app.schemas import DialogueSegment


def _segment(turn_id: str, index: int) -> DialogueSegment:
    return DialogueSegment(turn_id=turn_id, index=index, text=f"segment {index}")


def test_segment_decorator_freezes_one_presentation_per_turn_and_is_bounded() -> None:
    clock = FakeClock(datetime(2026, 7, 29, tzinfo=UTC))
    engine = SimpleNamespace(state=SimpleNamespace(dominant_label=EmotionLabel.happy))
    decorator = EmotionSegmentDecorator(
        cast(EmotionEngine, engine),
        ExpressionCooldown(clock=clock, cooldown=timedelta(0)),
        maximum_turns=2,
    )

    first = decorator(_segment("turn_a", 0))
    engine.state.dominant_label = EmotionLabel.focused
    second = decorator(_segment("turn_a", 1))
    other = decorator(_segment("turn_b", 0))

    assert first.emotion == second.emotion == "happy"
    assert first.tts_style == second.tts_style
    assert other.emotion == "focused"

    decorator(_segment("turn_c", 0))
    assert decorator.cached_turn_count == 2
