"""Pure output mapping plus a device-independent expression cooldown gate."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.emotion.clock import Clock, SystemClock
from app.emotion.models import EmotionLabel, EmotionPresentation

_PRESENTATIONS: dict[EmotionLabel, tuple[str, float, str]] = {
    EmotionLabel.neutral: ("default", 1.00, "neutral"),
    EmotionLabel.happy: ("bright", 1.05, "smile"),
    EmotionLabel.shy: ("soft_shy", 0.95, "blush"),
    EmotionLabel.proud: ("proud", 1.03, "proud_smirk"),
    EmotionLabel.angry_cute: ("tsundere", 1.08, "pout"),
    EmotionLabel.worried: ("gentle", 0.92, "worried"),
    EmotionLabel.bored: ("soft", 0.93, "bored"),
    EmotionLabel.excited: ("bright", 1.08, "excited"),
    EmotionLabel.explosion_mode: ("excited_explosion", 1.12, "explosion_excited"),
    EmotionLabel.sleepy: ("sleepy", 0.85, "sleepy"),
    EmotionLabel.focused: ("calm", 0.95, "focused"),
}


def map_presentation(label: EmotionLabel) -> EmotionPresentation:
    style, speed, expression = _PRESENTATIONS[label]
    return EmotionPresentation(
        label=label,
        tts_style=style,
        speed_factor=speed,
        vts_expression=expression,
    )


class ExpressionCooldown:
    """Rate-limit semantic expression changes without knowing anything about VTS."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        cooldown: timedelta = timedelta(seconds=4),
    ) -> None:
        if cooldown.total_seconds() < 0:
            raise ValueError("expression cooldown cannot be negative")
        self._clock = clock or SystemClock()
        self._cooldown = cooldown
        self._last_emitted_at: datetime | None = None
        self._last_expression: str | None = None

    def allow(self, presentation: EmotionPresentation, *, at: datetime | None = None) -> bool:
        now = at or self._clock.now()
        if presentation.vts_expression == self._last_expression:
            return False
        if self._last_emitted_at is not None and now - self._last_emitted_at < self._cooldown:
            return False
        self._last_expression = presentation.vts_expression
        self._last_emitted_at = now
        return True
