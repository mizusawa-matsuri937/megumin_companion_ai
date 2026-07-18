"""Deterministic final authority over LLM-proposed long-term memories."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from app.memory.models import (
    ApprovedMemory,
    MemoryDecision,
    MemoryEvaluation,
    MemoryProposal,
    MemorySensitivity,
    MemoryType,
    SourceInputMode,
    SourceProvenance,
)
from app.memory.privacy import classify_sensitivity, contains_credential

_EXPLICIT_REMEMBER = re.compile(r"(?i)(?:请记住|记住这个|remember this|please remember)")
_TRANSIENT = re.compile(r"(?i)(?:现在有点|今天有点|刚刚|临时|暂时|right now|just now|temporary)")


class MemoryPolicy:
    def __init__(
        self,
        *,
        importance_threshold: float = 0.55,
        confidence_threshold: float = 0.80,
    ) -> None:
        if not 0 <= importance_threshold <= 1 or not 0 <= confidence_threshold <= 1:
            raise ValueError("memory thresholds must be in [0, 1]")
        self.importance_threshold = importance_threshold
        self.confidence_threshold = confidence_threshold

    def evaluate(self, proposal: MemoryProposal) -> MemoryEvaluation:
        claim = proposal.claim
        normalized = normalize_memory_text(claim.content)
        effective_importance = claim.importance_score
        if _EXPLICIT_REMEMBER.search(proposal.source_text):
            effective_importance = min(1.0, effective_importance + 0.10)

        combined = "\n".join((proposal.source_text, claim.content, claim.evidence_quote))
        detected = classify_sensitivity(combined)
        sensitivity = _most_sensitive(detected, claim.sensitivity_hint)
        if contains_credential(combined) or sensitivity is MemorySensitivity.credential:
            return self._evaluation(
                proposal,
                MemoryDecision.reject,
                "credential_forbidden",
                normalized,
                effective_importance,
                MemorySensitivity.credential,
            )
        if claim.memory_type is MemoryType.user_profile and not claim.canonical_key.startswith(
            "profile:"
        ):
            return self._evaluation(
                proposal,
                MemoryDecision.reject,
                "invalid_profile_key",
                normalized,
                effective_importance,
                sensitivity,
            )
        if claim.memory_type in {MemoryType.fact, MemoryType.emotion} and _TRANSIENT.search(
            proposal.source_text
        ):
            return self._evaluation(
                proposal,
                MemoryDecision.reject,
                "transient_state",
                normalized,
                effective_importance,
                sensitivity,
            )
        if effective_importance < self.importance_threshold:
            return self._evaluation(
                proposal,
                MemoryDecision.reject,
                "importance_below_threshold",
                normalized,
                effective_importance,
                sensitivity,
            )
        if claim.confidence_score < self.confidence_threshold:
            return self._evaluation(
                proposal,
                MemoryDecision.reject,
                "confidence_below_threshold",
                normalized,
                effective_importance,
                sensitivity,
            )
        if sensitivity is not MemorySensitivity.normal:
            return self._evaluation(
                proposal,
                MemoryDecision.confirmation_required,
                "sensitive_fact_confirmation_required",
                normalized,
                effective_importance,
                sensitivity,
            )
        return self._evaluation(
            proposal,
            MemoryDecision.save,
            "policy_approved",
            normalized,
            effective_importance,
            sensitivity,
        )

    def approve(self, evaluation: MemoryEvaluation) -> ApprovedMemory:
        if evaluation.decision is MemoryDecision.reject:
            raise ValueError("a rejected evaluation cannot be approved")
        proposal = evaluation.proposal
        combined = "\n".join(
            (proposal.source_text, proposal.claim.content, proposal.claim.evidence_quote)
        )
        if contains_credential(combined):
            raise ValueError("credential content can never be approved")
        return ApprovedMemory(
            user_id=proposal.user_id,
            memory_type=proposal.claim.memory_type,
            canonical_key=proposal.claim.canonical_key,
            content=proposal.claim.content,
            normalized_content=evaluation.normalized_content,
            importance_score=evaluation.effective_importance,
            confidence_score=evaluation.effective_confidence,
            sensitivity=evaluation.sensitivity,
            source_kind=proposal.source_kind,
            source_message_id=proposal.source_message_id,
            evidence_quote=proposal.claim.evidence_quote,
            source_sha256=hashlib.sha256(proposal.source_text.encode("utf-8")).hexdigest(),
            provenance={
                SourceInputMode.text: SourceProvenance.dialogue_text,
                SourceInputMode.voice: SourceProvenance.dialogue_voice,
                SourceInputMode.manual: SourceProvenance.manual,
            }[proposal.source_input_mode],
            related_emotion=proposal.claim.related_emotion,
            created_at=proposal.created_at,
        )

    @staticmethod
    def _evaluation(
        proposal: MemoryProposal,
        decision: MemoryDecision,
        reason_code: str,
        normalized: str,
        effective_importance: float,
        sensitivity: MemorySensitivity,
    ) -> MemoryEvaluation:
        return MemoryEvaluation(
            proposal=proposal,
            decision=decision,
            reason_code=reason_code,
            normalized_content=normalized,
            effective_importance=effective_importance,
            effective_confidence=proposal.claim.confidence_score,
            sensitivity=sensitivity,
        )


def normalize_memory_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _most_sensitive(detected: MemorySensitivity, hint: MemorySensitivity) -> MemorySensitivity:
    order = {
        MemorySensitivity.normal: 0,
        MemorySensitivity.other_sensitive: 1,
        MemorySensitivity.health: 2,
        MemorySensitivity.address: 2,
        MemorySensitivity.credential: 3,
    }
    return detected if order[detected] >= order[hint] else hint
