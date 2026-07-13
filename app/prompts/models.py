"""Prompt-builder-only models layered on the shared AI contracts."""

from __future__ import annotations

from typing import Literal

from app.schemas.ai import ChatRequest, ChatRole
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    history_chars: int = Field(default=6_000, ge=0, le=100_000)
    context_chars: int = Field(default=4_000, ge=0, le=100_000)
    max_block_chars: int = Field(default=1_000, ge=64, le=20_000)


class PromptBuildResult(PromptModel):
    request: ChatRequest
    included_history_ids: tuple[str, ...] = ()
    included_context_ids: tuple[str, ...] = ()
    omitted_history_count: int = Field(default=0, ge=0)
    omitted_context_count: int = Field(default=0, ge=0)
