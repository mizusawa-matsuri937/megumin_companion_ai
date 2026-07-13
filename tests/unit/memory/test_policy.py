from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.memory.models import (
    MemoryActionResult,
    MemoryClaim,
    MemoryDecision,
    MemoryProposal,
    MemorySensitivity,
    MemoryType,
    SourceInputMode,
)
from app.memory.policy import MemoryPolicy, normalize_memory_text
from pydantic import ValidationError


def _claim(
    *,
    content: str = "用户喜欢手冲咖啡",
    evidence: str = "我喜欢手冲咖啡",
    importance: float = 0.8,
    confidence: float = 0.9,
    memory_type: MemoryType = MemoryType.fact,
    canonical_key: str = "preference:coffee",
    sensitivity: MemorySensitivity = MemorySensitivity.normal,
) -> MemoryClaim:
    return MemoryClaim(
        memory_type=memory_type,
        canonical_key=canonical_key,
        content=content,
        evidence_quote=evidence,
        importance_score=importance,
        confidence_score=confidence,
        sensitivity_hint=sensitivity,
    )


def _proposal(claim: MemoryClaim, source: str | None = None) -> MemoryProposal:
    return MemoryProposal(
        user_id="local_user",
        source_message_id="msg-1",
        source_input_mode=SourceInputMode.text,
        source_text=source or claim.evidence_quote,
        claim=claim,
        created_at=datetime(2026, 7, 13, tzinfo=UTC),
    )


def test_grounded_high_score_fact_is_approved() -> None:
    evaluation = MemoryPolicy().evaluate(_proposal(_claim()))

    assert evaluation.decision is MemoryDecision.save
    approved = MemoryPolicy().approve(evaluation)
    assert approved.source_message_id == "msg-1"
    assert approved.normalized_content == "用户喜欢手冲咖啡"
    with pytest.raises(ValidationError, match="does not match"):
        MemoryActionResult(evaluation=evaluation)


def test_policy_configuration_and_claim_text_are_strict() -> None:
    with pytest.raises(ValueError, match="thresholds"):
        MemoryPolicy(importance_threshold=-0.1)
    with pytest.raises(ValidationError, match="blank"):
        _claim(content="   ")


@pytest.mark.parametrize(
    ("importance", "confidence", "reason"),
    [
        (0.54, 0.99, "importance_below_threshold"),
        (0.99, 0.79, "confidence_below_threshold"),
    ],
)
def test_thresholds_are_final_authority(importance: float, confidence: float, reason: str) -> None:
    evaluation = MemoryPolicy().evaluate(
        _proposal(_claim(importance=importance, confidence=confidence))
    )
    assert evaluation.decision is MemoryDecision.reject
    assert evaluation.reason_code == reason


def test_explicit_remember_boost_cannot_bypass_other_gates() -> None:
    claim = _claim(importance=0.50)
    approved = MemoryPolicy().evaluate(_proposal(claim, "请记住：我喜欢手冲咖啡"))
    low_confidence = MemoryPolicy().evaluate(
        _proposal(_claim(importance=0.50, confidence=0.70), "请记住：我喜欢手冲咖啡")
    )

    assert approved.decision is MemoryDecision.save
    assert approved.effective_importance == pytest.approx(0.60)
    assert low_confidence.reason_code == "confidence_below_threshold"


@pytest.mark.parametrize(
    "source",
    [
        "我的 password = fake-password-12345",
        "API key: sk-fake123456789012345678",
        "验证码是 123456",
        "测试卡号 4242 4242 4242 4242",
        "测试证件号 11010519491231002X",
    ],
)
def test_credentials_are_rejected_even_with_high_scores(source: str) -> None:
    claim = _claim(content=source, evidence=source, importance=1.0, confidence=1.0)
    evaluation = MemoryPolicy().evaluate(_proposal(claim, source))

    assert evaluation.decision is MemoryDecision.reject
    assert evaluation.reason_code == "credential_forbidden"
    with pytest.raises(ValueError, match="rejected"):
        MemoryPolicy().approve(evaluation)


def test_credential_hint_is_conservatively_rejected_and_manual_source_is_explicit() -> None:
    claim = _claim(sensitivity=MemorySensitivity.credential)
    proposal = _proposal(claim).model_copy(update={"source_input_mode": SourceInputMode.manual})
    evaluation = MemoryPolicy().evaluate(proposal)

    assert evaluation.reason_code == "credential_forbidden"
    assert proposal.source_kind.value == "manual"


@pytest.mark.parametrize(
    ("source", "sensitivity"),
    [
        ("我被诊断为测试性焦虑症", MemorySensitivity.health),
        ("我的家庭地址是测试路一号", MemorySensitivity.address),
    ],
)
def test_sensitive_personal_facts_require_confirmation(
    source: str, sensitivity: MemorySensitivity
) -> None:
    evaluation = MemoryPolicy().evaluate(_proposal(_claim(content=source, evidence=source), source))
    assert evaluation.decision is MemoryDecision.confirmation_required
    assert evaluation.sensitivity is sensitivity


def test_transient_state_is_not_long_term_memory() -> None:
    source = "我今天有点累"
    evaluation = MemoryPolicy().evaluate(
        _proposal(_claim(content=source, evidence=source, memory_type=MemoryType.emotion), source)
    )
    assert evaluation.reason_code == "transient_state"


def test_profile_claim_requires_profile_key() -> None:
    evaluation = MemoryPolicy().evaluate(
        _proposal(
            _claim(
                memory_type=MemoryType.user_profile,
                canonical_key="name",
            )
        )
    )
    assert evaluation.reason_code == "invalid_profile_key"


def test_source_evidence_and_provenance_are_hard_validations() -> None:
    payload = {
        "user_id": "local_user",
        "source_message_id": "msg",
        "source_input_mode": "screen",
        "source_text": "我喜欢咖啡",
        "claim": _claim(evidence="我喜欢咖啡"),
        "created_at": datetime(2026, 7, 13, tzinfo=UTC),
    }
    with pytest.raises(ValidationError):
        MemoryProposal.model_validate(payload)
    payload["source_input_mode"] = "proactive"
    with pytest.raises(ValidationError):
        MemoryProposal.model_validate(payload)
    payload["source_input_mode"] = "text"
    payload["source_text"] = "unrelated"
    with pytest.raises(ValidationError, match="evidence_quote"):
        MemoryProposal.model_validate(payload)
    payload["source_text"] = "我喜欢咖啡"
    payload["created_at"] = datetime(2026, 7, 13)
    with pytest.raises(ValidationError, match="timezone-aware"):
        MemoryProposal.model_validate(payload)


def test_normalization_is_deterministic_for_dedup() -> None:
    assert normalize_memory_text(" ＡＢＣ  Coffee\n") == "abc coffee"
