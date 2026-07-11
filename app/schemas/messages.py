"""Message, turn, segmentation, audio, and observable pipeline contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def prefixed_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class InputMode(StrEnum):
    text = "text"
    voice = "voice"


class TurnStatus(StrEnum):
    accepted = "accepted"
    streaming = "streaming"
    speaking = "speaking"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class UserMessage(ContractModel):
    message_id: str = Field(default_factory=lambda: prefixed_id("msg"), min_length=1)
    session_id: str = Field(default="local_session", min_length=1)
    user_id: str = Field(default="local_user", min_length=1)
    text: str = Field(min_length=1, max_length=20_000)
    input_mode: InputMode = InputMode.text
    created_at: datetime = Field(default_factory=utc_now)
    interruption_policy: Literal["stop_now", "finish_sentence", "ignore"] = "stop_now"
    screen_context_allowed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text 不能只包含空白字符")
        return normalized


class DialogueSegment(ContractModel):
    segment_id: str = Field(default_factory=lambda: prefixed_id("seg"), min_length=1)
    turn_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    text: str = Field(min_length=1)
    emotion: str = "neutral"
    tts_style: str = "default"
    live2d_expression: str = "neutral"
    interruptible: bool = True
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text 不能只包含空白字符")
        return value


class TTSJob(ContractModel):
    job_id: str = Field(default_factory=lambda: prefixed_id("tts"), min_length=1)
    turn_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    style: str = "default"
    emotion: str = "neutral"
    speed_factor: float = Field(default=1.0, gt=0.0, le=3.0)
    interruptible: bool = True
    timeout_ms: int = Field(default=8000, gt=0)
    created_at: datetime = Field(default_factory=utc_now)
    cancellation_token_id: str = Field(min_length=1)


class AudioResult(ContractModel):
    audio_id: str = Field(default_factory=lambda: prefixed_id("audio"), min_length=1)
    job_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    success: bool
    audio_path: Path | None = None
    sample_rate: int | None = Field(default=None, gt=0)
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    ready_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_result_shape(self) -> AudioResult:
        if self.success and self.audio_path is None:
            raise ValueError("成功的 AudioResult 必须包含 audio_path")
        if not self.success and not self.error_code:
            raise ValueError("失败的 AudioResult 必须包含 error_code")
        return self


class TurnState(ContractModel):
    turn_id: str = Field(default_factory=lambda: prefixed_id("turn"), min_length=1)
    session_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    input_mode: InputMode
    status: TurnStatus = TurnStatus.accepted
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error_code: str | None = None


class TurnMetrics(ContractModel):
    """Privacy-safe latency measurements for one dialogue turn."""

    llm_first_token_ms: int | None = Field(default=None, ge=0)
    llm_first_segment_ms: int | None = Field(default=None, ge=0)
    tts_first_audio_ms: int | None = Field(default=None, ge=0)
    first_sentence_play_ms: int | None = Field(default=None, ge=0)
    turn_total_ms: int | None = Field(default=None, ge=0)
    tts_job_latency_ms: list[int] = Field(default_factory=list)
    audio_queue_wait_ms: list[int] = Field(default_factory=list)
    segment_count: int = Field(default=0, ge=0)
    playback_count: int = Field(default=0, ge=0)


class PipelineEvent(ContractModel):
    """Event sent to a local client while a turn is running."""

    type: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class TurnInterruptRequest(ContractModel):
    turn_id: str | None = Field(default=None, min_length=1)
    session_id: str = Field(default="local_session", min_length=1)
