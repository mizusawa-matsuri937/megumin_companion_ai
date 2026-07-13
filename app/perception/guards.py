"""Deterministic pre-capture and post-OCR privacy guards."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from app.perception.models import (
    ContentGuardDecision,
    GuardOutcome,
    OCRResult,
    Rect,
    WindowGuardDecision,
    WindowInfo,
)

DEFAULT_SENSITIVE_PROCESSES = (
    "1password",
    "bitwarden",
    "keepass",
    "wechat",
    "telegram",
    "signal",
    "whatsapp",
    "outlook",
    "thunderbird",
    "alipay",
    "anydesk",
    "teamviewer",
    "mstsc",
)

DEFAULT_SENSITIVE_TITLES = (
    "password",
    "passcode",
    "sign in",
    "login",
    "incognito",
    "private browsing",
    "checkout",
    "payment",
    "bank",
    "remote desktop",
    "密码",
    "验证码",
    "登录",
    "无痕",
    "支付",
    "银行",
    "身份证",
    "远程桌面",
)


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _fingerprint(value: str) -> str:
    return hashlib.sha256(_normalized(value).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PreCaptureGuard:
    sensitive_processes: tuple[str, ...] = DEFAULT_SENSITIVE_PROCESSES
    sensitive_title_keywords: tuple[str, ...] = DEFAULT_SENSITIVE_TITLES

    async def evaluate(self, window: WindowInfo) -> WindowGuardDecision:
        process = _normalized(window.process_name)
        title = _normalized(window.title)
        fingerprint = _fingerprint(window.process_name)
        if any(_normalized(candidate) in process for candidate in self.sensitive_processes):
            return WindowGuardDecision(
                outcome=GuardOutcome.block,
                category="sensitive",
                reason_code="sensitive_process",
                process_fingerprint=fingerprint,
            )
        if any(_normalized(keyword) in title for keyword in self.sensitive_title_keywords):
            return WindowGuardDecision(
                outcome=GuardOutcome.block,
                category="sensitive",
                reason_code="sensitive_title",
                process_fingerprint=fingerprint,
            )
        return WindowGuardDecision(
            outcome=GuardOutcome.allow,
            category="unknown",
            reason_code="guard_passed",
            process_fingerprint=fingerprint,
        )


_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "api_key",
        re.compile(r"(?i)\b(?:sk|rk|pk|ghp|github_pat|xox[baprs])[-_][a-z0-9_-]{12,}\b"),
    ),
    (
        "email",
        re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+\b"),
    ),
    ("phone", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
    ("long_number", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
    (
        "credential_context",
        re.compile(r"(?i)(?:password|passcode|otp|验证码|密码)\s*[:：=]\s*\S+"),
    ),
)


@dataclass(frozen=True, slots=True)
class OCRContentGuard:
    sensitive_keywords: tuple[str, ...] = (
        "private key",
        "recovery phrase",
        "seed phrase",
        "银行卡",
        "信用卡",
        "身份证",
        "支付密码",
    )
    max_text_chars: int = 100_000

    def evaluate(self, ocr: OCRResult) -> ContentGuardDecision:
        text = ocr.text[: self.max_text_chars]
        normalized = _normalized(text)
        reasons = {
            reason for reason, pattern in _CONTENT_PATTERNS if pattern.search(text) is not None
        }
        if any(_normalized(keyword) in normalized for keyword in self.sensitive_keywords):
            reasons.add("sensitive_keyword")
        if not reasons:
            return ContentGuardDecision(outcome=GuardOutcome.allow)
        regions: list[Rect] = []
        for span in ocr.spans:
            if span.box is None:
                continue
            if any(pattern.search(span.text) is not None for _reason, pattern in _CONTENT_PATTERNS):
                regions.append(span.box)
        return ContentGuardDecision(
            outcome=GuardOutcome.block,
            reason_codes=tuple(sorted(reasons)),
            redaction_regions=tuple(regions),
        )


class TextRedactor:
    """Remove control characters and replace common PII in provider summaries."""

    def __init__(self, *, max_chars: int = 2_000) -> None:
        if max_chars < 1:
            raise ValueError("max_chars 必须大于 0")
        self._max_chars = max_chars

    def redact(self, value: str) -> str:
        result = "".join(character for character in value if character.isprintable())
        replacements = {
            "api_key": "[SECRET]",
            "email": "[EMAIL]",
            "phone": "[PHONE]",
            "long_number": "[NUMBER]",
            "credential_context": "[CREDENTIAL]",
        }
        for reason, pattern in _CONTENT_PATTERNS:
            result = pattern.sub(replacements[reason], result)
        result = " ".join(result.split())[: self._max_chars].strip()
        return result or "屏幕场景已完成隐私处理。"
