from datetime import UTC, datetime, timedelta

import pytest
from app.clients.vts.expression_mapper import DEFAULT_EXPRESSION_HOTKEYS
from app.emotion.clock import FakeClock
from app.emotion.mapper import ExpressionCooldown, map_presentation
from app.emotion.models import EmotionLabel


def test_all_labels_have_complete_output_mapping() -> None:
    presentations = [map_presentation(label) for label in EmotionLabel]

    assert {item.label for item in presentations} == set(EmotionLabel)
    assert all(item.tts_style for item in presentations)
    assert all(item.vts_expression for item in presentations)
    assert all(item.vts_expression in DEFAULT_EXPRESSION_HOTKEYS for item in presentations)
    assert all(0 < item.speed_factor <= 3 for item in presentations)


def test_expression_gate_suppresses_duplicates_and_fast_changes() -> None:
    clock = FakeClock(datetime(2026, 7, 13, 12, tzinfo=UTC))
    gate = ExpressionCooldown(clock=clock, cooldown=timedelta(seconds=4))

    proud = map_presentation(EmotionLabel.proud)
    shy = map_presentation(EmotionLabel.shy)
    assert gate.allow(proud)
    assert not gate.allow(proud)
    assert not gate.allow(shy)
    clock.advance(timedelta(seconds=4))
    assert gate.allow(shy)


def test_expression_gate_rejects_negative_cooldown() -> None:
    with pytest.raises(ValueError, match="negative"):
        ExpressionCooldown(cooldown=timedelta(seconds=-1))
