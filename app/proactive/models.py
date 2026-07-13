"""Privacy-safe inputs and decisions for proactive dialogue."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from app.schemas import PerceptionContext, ProactiveIntent


class ProactiveTriggerType(StrEnum):
    idle = "idle"
    task_complete = "task_complete"
    emotion_shift = "emotion_shift"
    visual_change = "visual_change"
    scheduled = "scheduled"


class ProactiveSuppression(StrEnum):
    allowed = "allowed"
    disabled = "disabled"
    user_active = "user_active"
    focus_mode = "focus_mode"
    sensitive = "sensitive"
    do_not_disturb = "do_not_disturb"
    quiet_hours = "quiet_hours"
    cooldown = "cooldown"
    daily_limit = "daily_limit"
    insufficient_idle = "insufficient_idle"
    below_threshold = "below_threshold"


_DEFAULT_WEIGHTS = MappingProxyType(
    {
        ProactiveTriggerType.idle: 0.45,
        ProactiveTriggerType.task_complete: 0.75,
        ProactiveTriggerType.emotion_shift: 0.65,
        ProactiveTriggerType.visual_change: 0.55,
        ProactiveTriggerType.scheduled: 0.7,
    }
)


@dataclass(frozen=True, slots=True)
class ProactivePolicy:
    timezone: str = "Asia/Shanghai"
    minimum_score: float = 0.62
    cooldown: timedelta = timedelta(minutes=20)
    idle_minimum: timedelta = timedelta(minutes=3)
    daily_limit: int = 8
    quiet_start_hour: int = 23
    quiet_end_hour: int = 8
    trigger_weights: Mapping[ProactiveTriggerType, float] = field(
        default_factory=lambda: _DEFAULT_WEIGHTS
    )

    def __post_init__(self) -> None:
        if not 0.0 <= self.minimum_score <= 1.0:
            raise ValueError("minimum_score 必须位于 0..1")
        if self.cooldown < timedelta(0) or self.idle_minimum < timedelta(0):
            raise ValueError("cooldown/idle_minimum 不能为负")
        if self.daily_limit < 1:
            raise ValueError("daily_limit 必须大于 0")
        if not 0 <= self.quiet_start_hour <= 23 or not 0 <= self.quiet_end_hour <= 23:
            raise ValueError("安静时段小时必须位于 0..23")
        if set(self.trigger_weights) != set(ProactiveTriggerType):
            raise ValueError("trigger_weights 必须覆盖全部 trigger type")
        if any(not 0.0 <= value <= 1.0 for value in self.trigger_weights.values()):
            raise ValueError("trigger weight 必须位于 0..1")


@dataclass(frozen=True, slots=True)
class ProactiveTrigger:
    trigger_type: ProactiveTriggerType
    instruction: str
    confidence: float
    novelty: float = 0.5
    urgency: float = 0.0
    voice_allowed: bool = True
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.instruction.strip():
            raise ValueError("instruction 不能为空")
        for name, value in (
            ("confidence", self.confidence),
            ("novelty", self.novelty),
            ("urgency", self.urgency),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} 必须位于 0..1")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at 必须包含时区")


@dataclass(frozen=True, slots=True)
class ProactiveContext:
    now: datetime
    enabled: bool = False
    user_turn_active: bool = False
    focus_mode: bool = False
    sensitive: bool = False
    do_not_disturb: bool = False
    last_user_activity: datetime | None = None
    last_proactive_at: datetime | None = None
    proactive_today: int = 0
    perception: PerceptionContext | None = None

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("now 必须包含时区")
        for value in (self.last_user_activity, self.last_proactive_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("活动时间必须包含时区")
        if self.proactive_today < 0:
            raise ValueError("proactive_today 不能为负")


@dataclass(frozen=True, slots=True)
class ProactiveDecision:
    score: float
    suppression: ProactiveSuppression
    intent: ProactiveIntent | None = None
