"""Provider-neutral emotion domain contracts.

The models in this module intentionally contain no raw dialogue or screen text.  Callers
translate trusted, high-level observations into :class:`EmotionStimulus` values before
touching the state machine.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EmotionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class EmotionDimension(StrEnum):
    affection = "affection"
    energy = "energy"
    curiosity = "curiosity"
    concern = "concern"
    embarrassment = "embarrassment"
    pride = "pride"
    explosion_urge = "explosion_urge"
    boredom = "boredom"


class EmotionLabel(StrEnum):
    neutral = "neutral"
    happy = "happy"
    shy = "shy"
    proud = "proud"
    angry_cute = "angry_cute"
    worried = "worried"
    bored = "bored"
    excited = "excited"
    explosion_mode = "explosion_mode"
    sleepy = "sleepy"
    focused = "focused"


class StimulusKind(StrEnum):
    praise = "praise"
    user_distress = "user_distress"
    user_tired = "user_tired"
    explosion_topic = "explosion_topic"
    quiet_request = "quiet_request"
    repeated_interruption = "repeated_interruption"
    focused_activity = "focused_activity"
    gaming = "gaming"
    late_night = "late_night"
    inactivity = "inactivity"
    neutral_interaction = "neutral_interaction"
    time_decay = "time_decay"


class EmotionState(EmotionModel):
    affection: float = Field(default=0.35, ge=0.0, le=1.0)
    energy: float = Field(default=0.60, ge=0.0, le=1.0)
    curiosity: float = Field(default=0.50, ge=0.0, le=1.0)
    concern: float = Field(default=0.15, ge=0.0, le=1.0)
    embarrassment: float = Field(default=0.10, ge=0.0, le=1.0)
    pride: float = Field(default=0.55, ge=0.0, le=1.0)
    explosion_urge: float = Field(default=0.45, ge=0.0, le=1.0)
    boredom: float = Field(default=0.20, ge=0.0, le=1.0)
    dominant_label: EmotionLabel = EmotionLabel.neutral
    intensity: float = Field(default=0.30, ge=0.0, le=1.0)
    last_updated_at: datetime
    label_since: datetime
    reason_code: str | None = Field(default=None, max_length=80)

    @field_validator("last_updated_at", "label_since")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("emotion timestamps must be timezone-aware")
        return value

    def value(self, dimension: EmotionDimension) -> float:
        value = getattr(self, dimension.value)
        assert isinstance(value, float)
        return value


class EmotionStimulus(EmotionModel):
    stimulus_id: str = Field(min_length=1, max_length=128)
    kind: StimulusKind
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)
    occurred_at: datetime
    correlation_id: str | None = Field(default=None, max_length=128)
    reason_code: str = Field(min_length=1, max_length=80)

    @field_validator("occurred_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stimulus occurred_at must be timezone-aware")
        return value


class EmotionSuggestion(EmotionModel):
    """Untrusted LLM hint; the engine still selects and bounds every numeric transition."""

    suggestion_id: str = Field(min_length=1, max_length=128)
    kind: StimulusKind
    confidence: float = Field(ge=0.0, le=1.0)
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("suggestion occurred_at must be timezone-aware")
        return value


class EmotionTransition(EmotionModel):
    stimulus_id: str
    kind: StimulusKind
    before: EmotionState
    after: EmotionState
    requested_delta: dict[EmotionDimension, float]
    applied_delta: dict[EmotionDimension, float]
    limited_dimensions: frozenset[EmotionDimension] = frozenset()
    label_changed: bool = False
    label_change_suppressed: bool = False
    reason_code: str


class EmotionPresentation(EmotionModel):
    label: EmotionLabel
    tts_style: str = Field(min_length=1, max_length=64)
    speed_factor: float = Field(gt=0.0, le=3.0)
    vts_expression: str = Field(min_length=1, max_length=64)
