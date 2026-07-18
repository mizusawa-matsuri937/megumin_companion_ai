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


class CleanupKind(StrEnum):
    memory_delete = "memory_delete"
    memory_clear = "memory_clear"
    history_clear = "history_clear"
    history_retention = "history_retention"
    evidence_migration = "evidence_migration"


class CleanupState(StrEnum):
    pending = "pending"
    retrying = "retrying"
    completed = "completed"


class CleanupJob(StorageRecord):
    cleanup_id: str
    kind: CleanupKind
    state: CleanupState
    vacuum_required: bool
    attempt_count: int = Field(ge=0)
    reason_code: str | None = None
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime
    completed_at: datetime | None = None


class DeletionResult(StorageRecord):
    deleted_count: int = Field(ge=0)
    cleanup_id: str | None = None
    cleanup_state: CleanupState | None = None

    @property
    def logical_deleted(self) -> bool:
        return self.deleted_count > 0

    @property
    def cleanup_pending(self) -> bool:
        return self.cleanup_state in {CleanupState.pending, CleanupState.retrying}


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
