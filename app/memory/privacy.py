"""Conservative deterministic credential and sensitive-fact detection."""

from __future__ import annotations

import re

from app.memory.models import MemorySensitivity

_ASSIGNMENT_SECRET = re.compile(
    r"(?i)(?:password|passcode|passwd|pwd|api[ _-]?key|access[ _-]?token|secret|"
    r"密码|口令|验证码|密钥)\s*(?:[:=]|是)\s*\S{4,}"
)
_KNOWN_TOKEN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|"
    r"AIza[0-9A-Za-z_-]{20,})"
)
_VERIFICATION_CODE = re.compile(r"(?i)(?:验证码|verification code|otp)\D{0,12}\d{4,8}\b")
_IDENTITY_NUMBER = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

_HEALTH = re.compile(
    r"(?i)(?:诊断|病史|处方|抑郁|焦虑症|糖尿病|高血压|癌症|"
    r"diagnosed|medical history|prescription|medication)"
)
_ADDRESS = re.compile(
    r"(?i)(?:住址|家庭地址|收货地址|家住|居住在|\baddress\b|\bstreet\b|\bavenue\b)"
)


def contains_credential(text: str) -> bool:
    if any(
        pattern.search(text)
        for pattern in (_ASSIGNMENT_SECRET, _KNOWN_TOKEN, _VERIFICATION_CODE, _IDENTITY_NUMBER)
    ):
        return True
    for match in _CARD_CANDIDATE.finditer(text):
        digits = "".join(character for character in match.group() if character.isdigit())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return True
    return False


def classify_sensitivity(text: str) -> MemorySensitivity:
    if contains_credential(text):
        return MemorySensitivity.credential
    if _HEALTH.search(text):
        return MemorySensitivity.health
    if _ADDRESS.search(text):
        return MemorySensitivity.address
    return MemorySensitivity.normal


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
