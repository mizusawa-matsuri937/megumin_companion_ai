"""Memory-domain contracts with explicit provenance and confirmation state."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from app.emotion.models import EmotionLabel
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    @field_validator("*", mode="after")
    @classmethod
    def reject_naive_datetimes(cls, value: object) -> object:
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("memory timestamps must be timezone-aware")
        return value


class MemoryType(StrEnum):
    user_profile = "user_profile"
    fact = "fact"
    event = "event"
    emotion = "emotion"
    relationship = "relationship"


class MemorySensitivity(StrEnum):
    normal = "normal"
    health = "health"
    address = "address"
    other_sensitive = "other_sensitive"
    credential = "credential"


class MemoryStatus(StrEnum):
    active = "active"
    superseded = "superseded"


class MemorySourceKind(StrEnum):
    dialogue = "dialogue"
    manual = "manual"


class SourceInputMode(StrEnum):
    """The exhaustive provenance allow-list; screen and proactive are deliberately absent."""

    text = "text"
    voice = "voice"
    manual = "manual"


class MemoryDecision(StrEnum):
    reject = "reject"
    save = "save"
    confirmation_required = "confirmation_required"


class MemoryClaim(MemoryModel):
    """Structured claim proposed by an LLM; it contains no authoritative provenance."""

    memory_type: MemoryType
    canonical_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{1,127}$")
    content: str = Field(min_length=1, max_length=5_000)
    evidence_quote: str = Field(min_length=1, max_length=2_000)
    importance_score: float = Field(ge=0.0, le=1.0)
    confidence_score: float = Field(ge=0.0, le=1.0)
    sensitivity_hint: MemorySensitivity = MemorySensitivity.normal
    related_emotion: EmotionLabel | None = None

    @field_validator("content", "evidence_quote")
    @classmethod
    def normalize_visible_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("memory text cannot be blank")
        return normalized


class MemoryProposal(MemoryModel):
    proposal_id: str = Field(default_factory=lambda: _id("proposal"))
    user_id: str = Field(min_length=1, max_length=128)
    source_message_id: str = Field(min_length=1, max_length=128)
    source_input_mode: SourceInputMode
    source_text: str = Field(min_length=1, max_length=20_000)
    claim: MemoryClaim
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("memory timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def evidence_must_come_from_source(self) -> MemoryProposal:
        if (
            self.claim.evidence_quote.casefold()
            not in " ".join(self.source_text.split()).casefold()
        ):
            raise ValueError("evidence_quote must be present in the explicit source message")
        return self

    @property
    def source_kind(self) -> MemorySourceKind:
        if self.source_input_mode is SourceInputMode.manual:
            return MemorySourceKind.manual
        return MemorySourceKind.dialogue


class MemoryEvaluation(MemoryModel):
    proposal: MemoryProposal
    decision: MemoryDecision
    reason_code: str = Field(min_length=1, max_length=80)
    normalized_content: str
    effective_importance: float = Field(ge=0.0, le=1.0)
    effective_confidence: float = Field(ge=0.0, le=1.0)
    sensitivity: MemorySensitivity


class ApprovedMemory(MemoryModel):
    memory_id: str = Field(default_factory=lambda: _id("mem"))
    user_id: str
    memory_type: MemoryType
    canonical_key: str
    content: str
    normalized_content: str
    importance_score: float = Field(ge=0.0, le=1.0)
    confidence_score: float = Field(ge=0.0, le=1.0)
    sensitivity: MemorySensitivity
    source_kind: MemorySourceKind
    source_message_id: str
    source_excerpt: str
    related_emotion: EmotionLabel | None = None
    created_at: datetime


class MemoryItem(MemoryModel):
    memory_id: str
    user_id: str
    memory_type: MemoryType
    canonical_key: str
    content: str
    normalized_content: str
    importance_score: float = Field(ge=0.0, le=1.0)
    confidence_score: float = Field(ge=0.0, le=1.0)
    sensitivity: MemorySensitivity
    status: MemoryStatus
    source_kind: MemorySourceKind
    source_message_id: str
    source_excerpt: str
    related_emotion: EmotionLabel | None = None
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime


class MemorySource(MemoryModel):
    source_id: str
    memory_id: str
    source_kind: MemorySourceKind
    source_message_id: str
    source_excerpt: str
    observed_at: datetime


class ProfileItem(MemoryModel):
    user_id: str
    profile_key: str
    memory_id: str
    value: str
    created_at: datetime
    updated_at: datetime


class PendingConfirmation(MemoryModel):
    confirmation_id: str = Field(default_factory=lambda: _id("confirm"))
    evaluation: MemoryEvaluation
    expires_at: datetime


class MemoryActionResult(MemoryModel):
    evaluation: MemoryEvaluation
    item: MemoryItem | None = None
    confirmation: PendingConfirmation | None = None

    @model_validator(mode="after")
    def result_matches_decision(self) -> MemoryActionResult:
        if self.evaluation.decision is MemoryDecision.reject:
            valid = self.item is None and self.confirmation is None
        elif self.evaluation.decision is MemoryDecision.save:
            valid = self.item is not None and self.confirmation is None
        else:
            valid = self.item is None and self.confirmation is not None
        if not valid:
            raise ValueError("memory action payload does not match its decision")
        return self
