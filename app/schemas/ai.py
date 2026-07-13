"""Provider-neutral AI, context, feature, and turn outcome contracts."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.messages import (
    ContractModel,
    DialogueSegment,
    TurnMetrics,
    prefixed_id,
    utc_now,
)

_PROACTIVE_OBJECTIVES = {
    "idle": "用户已一段时间没有互动；生成一句简短、低打扰的陪伴式问候。",
    "task_complete": "用户刚完成一项任务；生成一句简短、克制的祝贺。",
    "emotion_shift": "系统检测到情绪状态变化；生成一句不作诊断的温和关心。",
    "visual_change": "系统检测到已通过隐私检查的普通场景变化；生成一句不引用屏幕内容的简短回应。",
    "scheduled": "执行一次用户预先允许的简短、低打扰问候。",
}


class ChatRole(StrEnum):
    system = "system"
    user = "user"
    assistant = "assistant"


class TextContent(ContractModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=100_000)


class ImageURLContent(ContractModel):
    type: Literal["image_url"] = "image_url"
    url: str = Field(min_length=1)
    detail: Literal["low", "high", "auto"] = "low"

    @field_validator("url")
    @classmethod
    def validate_image_url(cls, value: str) -> str:
        if value.startswith(("https://", "http://")):
            return value
        prefix, separator, encoded = value.partition(",")
        if not separator or prefix not in {
            "data:image/jpeg;base64",
            "data:image/png;base64",
            "data:image/webp;base64",
        }:
            raise ValueError("image_url 必须是 HTTP(S) URL 或受支持的图片 data URL")
        try:
            base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("image_url 包含无效 base64") from exc
        return value


ChatContent = Annotated[TextContent | ImageURLContent, Field(discriminator="type")]


class ChatMessage(ContractModel):
    role: ChatRole
    content: str | list[ChatContent]

    @model_validator(mode="after")
    def reject_empty_content(self) -> ChatMessage:
        if isinstance(self.content, str):
            if not self.content.strip():
                raise ValueError("chat content 不能为空")
        elif not self.content:
            raise ValueError("chat content parts 不能为空")
        return self


class ChatRequest(ContractModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=100_000)
    response_format: Literal["text", "json_object"] = "text"


class ChatCompletion(ContractModel):
    text: str
    finish_reason: str | None = None
    model: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class ContextOrigin(StrEnum):
    recent_dialogue = "recent_dialogue"
    long_term_memory = "long_term_memory"
    user_profile = "user_profile"
    screen = "screen"
    proactive = "proactive"


class ContextTrust(StrEnum):
    user_statement = "user_statement"
    stored_fact = "stored_fact"
    untrusted_observation = "untrusted_observation"
    internal_intent = "internal_intent"


class ExternalContextBlock(ContractModel):
    origin: ContextOrigin
    trust: ContextTrust
    content: str = Field(min_length=1, max_length=20_000)
    persistable: bool = False
    source_id: str | None = None


class PerceptionContext(ContractModel):
    category: str = "unknown"
    summary: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    observation_id: str = Field(default_factory=lambda: prefixed_id("obs"))
    sensitive: bool = False
    observed_at: datetime = Field(default_factory=utc_now)
    generation: int = Field(default=0, ge=0)

    @field_validator("observed_at")
    @classmethod
    def require_aware_observation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("perception observed_at 必须包含时区")
        return value


class ProactiveIntent(ContractModel):
    intent_id: str = Field(default_factory=lambda: prefixed_id("intent"))
    trigger_type: Literal["idle", "task_complete", "emotion_shift", "visual_change", "scheduled"]
    instruction: str = Field(min_length=1, max_length=2_000)
    score: float = Field(ge=0.0, le=1.0)
    voice_allowed: bool = True
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")

    @model_validator(mode="after")
    def replace_caller_text_with_fixed_objective(self) -> ProactiveIntent:
        self.instruction = _PROACTIVE_OBJECTIVES[self.trigger_type]
        return self


class FeatureName(StrEnum):
    recent_history = "recent_history"
    long_term_memory = "long_term_memory"
    vision = "vision"
    cloud_vision = "cloud_vision"
    proactive = "proactive"


class FeatureState(ContractModel):
    name: FeatureName
    enabled: bool
    disclosure: str | None = None


class FeaturePatchRequest(ContractModel):
    enabled: bool


class MemoryUpdateRequest(ContractModel):
    content: str = Field(min_length=1, max_length=5_000)


class MemoryConfirmRequest(ContractModel):
    approved: bool


class HistoryClearRequest(ContractModel):
    user_id: str = Field(default="local_user", min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class TurnOutcome(ContractModel):
    full_text: str
    segments: list[DialogueSegment] = Field(default_factory=list)
    metrics: TurnMetrics
    metadata: dict[str, Any] = Field(default_factory=dict)
