"""Asset-neutral mapping from the current bounded emotion model to one turn plan."""

from __future__ import annotations

from app.avatar.models import AvatarTransitionClass, AvatarTurnPlan
from app.emotion import EmotionLabel

_VOICE_SLOTS: dict[EmotionLabel, str] = {
    EmotionLabel.neutral: "neutral",
    EmotionLabel.happy: "gentle",
    EmotionLabel.shy: "tsundere",
    EmotionLabel.proud: "focused",
    EmotionLabel.angry_cute: "tsundere",
    EmotionLabel.worried: "gentle",
    EmotionLabel.bored: "neutral",
    EmotionLabel.excited: "gentle",
    EmotionLabel.explosion_mode: "excited_explosion",
    EmotionLabel.sleepy: "neutral",
    EmotionLabel.focused: "focused",
}

_BODY_MOTION_KEYS: dict[EmotionLabel, str | None] = {
    EmotionLabel.neutral: None,
    EmotionLabel.happy: "happy",
    EmotionLabel.shy: "shy",
    EmotionLabel.proud: "proud",
    EmotionLabel.angry_cute: "angry_cute",
    EmotionLabel.worried: "worried",
    EmotionLabel.bored: "bored",
    EmotionLabel.excited: "happy",
    EmotionLabel.explosion_mode: "explosion",
    EmotionLabel.sleepy: "sleepy",
    EmotionLabel.focused: "focused",
}

_SUDDEN = frozenset(
    {
        EmotionLabel.shy,
        EmotionLabel.excited,
        EmotionLabel.explosion_mode,
    }
)
_GENTLE = frozenset(
    {
        EmotionLabel.worried,
        EmotionLabel.bored,
        EmotionLabel.sleepy,
    }
)


def map_avatar_turn_plan(turn_id: str, emotion: EmotionLabel) -> AvatarTurnPlan:
    """Freeze one content-free plan without accepting arbitrary model controls."""

    if not isinstance(emotion, EmotionLabel):
        raise ValueError("Avatar emotion is invalid")
    transition = (
        AvatarTransitionClass.sudden
        if emotion in _SUDDEN
        else AvatarTransitionClass.gentle
        if emotion in _GENTLE
        else AvatarTransitionClass.normal
    )
    return AvatarTurnPlan(
        turn_id=turn_id,
        emotion=emotion,
        voice_slot=_VOICE_SLOTS[emotion],
        body_motion_key=_BODY_MOTION_KEYS[emotion],
        transition=transition,
    )
