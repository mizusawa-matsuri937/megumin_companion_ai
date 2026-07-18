"""Prompt-builder-only models layered on the shared AI contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.ai import ChatRequest, ChatRole


class PromptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class HistoryMessage(PromptModel):
    message_id: str = Field(min_length=1, max_length=128)
    role: Literal[ChatRole.user, ChatRole.assistant]
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("history content cannot be blank")
        return value


class PromptBudget(PromptModel):
    total_tokens: int = Field(default=16_384, ge=256, le=100_000)
    system_tokens: int = Field(default=4_096, ge=128, le=100_000)
    current_user_tokens: int = Field(default=8_192, ge=32, le=100_000)
    history_tokens: int = Field(default=4_096, ge=0, le=100_000)
    memory_tokens: int = Field(default=2_048, ge=0, le=100_000)
    screen_tokens: int = Field(default=1_024, ge=0, le=100_000)
    max_block_tokens: int = Field(default=512, ge=16, le=20_000)
    provider_output_tokens: int = Field(default=600, ge=1, le=100_000)


class PromptBuildResult(PromptModel):
    request: ChatRequest
    included_history_ids: tuple[str, ...] = ()
    included_context_ids: tuple[str, ...] = ()
    omitted_history_count: int = Field(default=0, ge=0)
    omitted_context_count: int = Field(default=0, ge=0)
    estimated_prompt_tokens: int = Field(default=0, ge=0)
    token_estimator_profile: str = Field(min_length=1)
    degradation_steps: tuple[str, ...] = ()
    current_user_truncated: bool = False
