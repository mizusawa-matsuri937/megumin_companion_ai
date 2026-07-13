"""Apply the current emotion presentation to provider-neutral dialogue segments."""

from __future__ import annotations

from app.emotion.engine import EmotionEngine
from app.emotion.mapper import ExpressionCooldown, map_presentation
from app.schemas import DialogueSegment


class EmotionSegmentDecorator:
    def __init__(self, engine: EmotionEngine, expression_cooldown: ExpressionCooldown) -> None:
        self._engine = engine
        self._expression_cooldown = expression_cooldown

    def __call__(self, segment: DialogueSegment) -> DialogueSegment:
        presentation = map_presentation(self._engine.state.dominant_label)
        return segment.model_copy(
            update={
                "emotion": presentation.label.value,
                "tts_style": presentation.tts_style,
                "tts_speed_factor": presentation.speed_factor,
                "live2d_expression": presentation.vts_expression,
                "expression_update": self._expression_cooldown.allow(presentation),
            }
        )
