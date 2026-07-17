"""Structured JSON logging with default-on sensitive-data redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.settings import Settings

REDACTED = "[REDACTED]"


class Redactor:
    """Redact credentials and common private identifiers recursively."""

    _patterns: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(
                r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
                r"password|passwd|authorization)\b(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
            ),
            rf"\1\2{REDACTED}",
        ),
        (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
        (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), REDACTED),
        (
            re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"),
            REDACTED,
        ),
        (re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), REDACTED),
        (
            re.compile(r"(?i)(验证码|verification[_ -]?code|otp)(\s*[:=：]?\s*)\d{4,8}"),
            rf"\1\2{REDACTED}",
        ),
        (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), REDACTED),
        (re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)"), REDACTED),
    )

    def __init__(self, known_secrets: Sequence[str] = ()) -> None:
        self._known_secrets = tuple(
            sorted((secret for secret in known_secrets if secret), key=len, reverse=True)
        )

    def redact_text(self, value: str) -> str:
        result = value
        for secret in self._known_secrets:
            result = result.replace(secret, REDACTED)
        for pattern, replacement in self._patterns:
            result = pattern.sub(replacement, result)
        return result

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "structured_fields", None)
        if isinstance(fields, Mapping):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(self._redactor.redact(payload), ensure_ascii=False, default=str)


def _handler(stream: Any, redactor: Redactor) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(redactor))
    return handler


def configure_logging(settings: Settings, *, file_path: Path | None = None) -> logging.Logger:
    """Configure the application logger without changing unrelated library loggers."""

    logger = logging.getLogger("megumin_companion")
    for existing_handler in logger.handlers:
        existing_handler.close()
    logger.handlers.clear()
    logger.setLevel(settings.app.log_level)
    logger.propagate = False
    redactor = Redactor(settings.known_secret_values())

    if settings.logging.console_enabled:
        logger.addHandler(_handler(sys.stdout, redactor))
    if settings.logging.file_enabled:
        target = file_path or settings.logging.file_path
        target = settings.resolve_runtime_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(target, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter(redactor))
        logger.addHandler(file_handler)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    logger.log(level, event, extra={"structured_fields": fields})
