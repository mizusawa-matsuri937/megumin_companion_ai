"""Persistence records that are not memory-policy decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StorageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class ConversationRole(StrEnum):
    user = "user"
    assistant = "assistant"


class ConversationOrigin(StrEnum):
    user_text = "user_text"
    user_voice = "user_voice"
    assistant_dialogue = "assistant_dialogue"
    assistant_proactive = "assistant_proactive"


class ConversationRecord(StorageRecord):
    message_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    role: ConversationRole
    origin: ConversationOrigin
    content: str = Field(min_length=1, max_length=20_000)
    created_at: datetime

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("conversation content cannot be blank")
        return normalized

    @model_validator(mode="after")
    def role_matches_origin(self) -> ConversationRecord:
        user_origins = {ConversationOrigin.user_text, ConversationOrigin.user_voice}
        if (self.role is ConversationRole.user) != (self.origin in user_origins):
            raise ValueError("conversation role does not match origin")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("conversation created_at must be timezone-aware")
        return self
