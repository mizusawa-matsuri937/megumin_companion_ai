"""Bounded emotion state, deterministic presentation mapping, and pipeline adapters."""

from app.emotion.clock import Clock, FakeClock, SystemClock
from app.emotion.engine import EmotionEngine
from app.emotion.integration import EmotionSegmentDecorator
from app.emotion.mapper import ExpressionCooldown, map_presentation
from app.emotion.models import (
    EmotionDimension,
    EmotionLabel,
    EmotionPresentation,
    EmotionState,
    EmotionStimulus,
    EmotionSuggestion,
    EmotionTransition,
    StimulusKind,
)

__all__ = [
    "Clock",
    "EmotionDimension",
    "EmotionEngine",
    "EmotionLabel",
    "EmotionPresentation",
    "EmotionSegmentDecorator",
    "EmotionState",
    "EmotionStimulus",
    "EmotionSuggestion",
    "EmotionTransition",
    "ExpressionCooldown",
    "FakeClock",
    "StimulusKind",
    "SystemClock",
    "map_presentation",
]
