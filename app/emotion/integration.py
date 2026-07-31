"""Apply the current emotion presentation to provider-neutral dialogue segments."""

from __future__ import annotations

from collections import OrderedDict

from app.emotion.engine import EmotionEngine
from app.emotion.mapper import ExpressionCooldown, map_presentation
from app.emotion.models import EmotionPresentation
from app.schemas import DialogueSegment


class EmotionSegmentDecorator:
    def __init__(
        self,
        engine: EmotionEngine,
        expression_cooldown: ExpressionCooldown,
        *,
        maximum_turns: int = 256,
    ) -> None:
        if (
            isinstance(maximum_turns, bool)
            or not isinstance(maximum_turns, int)
            or not 1 <= maximum_turns <= 2_048
        ):
            raise ValueError("emotion turn cache bound is invalid")
        self._engine = engine
        self._expression_cooldown = expression_cooldown
        self._maximum_turns = maximum_turns
        self._presentations: OrderedDict[str, EmotionPresentation] = OrderedDict()

    @property
    def cached_turn_count(self) -> int:
        return len(self._presentations)

    def __call__(self, segment: DialogueSegment) -> DialogueSegment:
        presentation = self._presentations.get(segment.turn_id)
        if presentation is None:
            presentation = map_presentation(self._engine.state.dominant_label)
            self._presentations[segment.turn_id] = presentation
            while len(self._presentations) > self._maximum_turns:
                self._presentations.popitem(last=False)
        else:
            self._presentations.move_to_end(segment.turn_id)
        return segment.model_copy(
            update={
                "emotion": presentation.label.value,
                "tts_style": presentation.tts_style,
                "tts_speed_factor": presentation.speed_factor,
                "live2d_expression": presentation.vts_expression,
                "expression_update": self._expression_cooldown.allow(presentation),
            }
        )
