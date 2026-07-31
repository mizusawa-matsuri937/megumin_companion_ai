"""Content-free W29 emotion, voice, motion, and transition routing."""

from __future__ import annotations

import pytest
from app.avatar import (
    AvatarTransitionClass,
    FocusedVariant,
    map_avatar_turn_plan,
)
from app.emotion import EmotionLabel


@pytest.mark.parametrize(
    ("emotion", "voice", "motion", "transition"),
    [
        (EmotionLabel.neutral, "neutral", None, AvatarTransitionClass.normal),
        (EmotionLabel.happy, "gentle", "happy", AvatarTransitionClass.normal),
        (EmotionLabel.shy, "tsundere", "shy", AvatarTransitionClass.sudden),
        (EmotionLabel.proud, "focused", "proud", AvatarTransitionClass.normal),
        (
            EmotionLabel.angry_cute,
            "tsundere",
            "angry_cute",
            AvatarTransitionClass.normal,
        ),
        (EmotionLabel.worried, "gentle", "worried", AvatarTransitionClass.gentle),
        (EmotionLabel.bored, "neutral", "bored", AvatarTransitionClass.gentle),
        (
            EmotionLabel.excited,
            "excited_explosion",
            "excited",
            AvatarTransitionClass.sudden,
        ),
        (
            EmotionLabel.explosion_mode,
            "excited_explosion",
            "explosion",
            AvatarTransitionClass.sudden,
        ),
        (EmotionLabel.sleepy, "neutral", "sleepy", AvatarTransitionClass.gentle),
        (EmotionLabel.focused, "focused", "focused", AvatarTransitionClass.normal),
    ],
)
def test_avatar_routing_table(
    emotion: EmotionLabel,
    voice: str,
    motion: str | None,
    transition: AvatarTransitionClass,
) -> None:
    plan = map_avatar_turn_plan("turn_test", emotion)

    assert plan.voice_slot == voice
    assert plan.body_motion_key == motion
    assert plan.transition is transition
    assert plan.focused_variant is FocusedVariant.default


def test_chuunibyou_focused_uses_local_semantic_and_invalid_pair_fails() -> None:
    plan = map_avatar_turn_plan(
        "turn_focus",
        EmotionLabel.focused,
        focused_variant=FocusedVariant.chuunibyou,
    )

    assert plan.body_motion_key == "focused_chuunibyou"
    assert plan.focused_variant is FocusedVariant.chuunibyou

    with pytest.raises(ValueError, match="Focused variant"):
        map_avatar_turn_plan(
            "turn_invalid",
            EmotionLabel.happy,
            focused_variant=FocusedVariant.chuunibyou,
        )
