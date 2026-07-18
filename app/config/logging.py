"""Bounded structured logging with default-on recursive privacy redaction."""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import logging.handlers
import math
import os
import re
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.settings import Settings
from app.windows_security import directory_security_for_current_platform

REDACTED = "[REDACTED]"
UNSERIALIZABLE = "[UNSERIALIZABLE]"
BINARY = "[BINARY]"
LOG_SCHEMA_VERSION = 1
MAX_LOG_RECORD_BYTES = 64 * 1024
MAX_LOG_VALUE_CHARS = 4_096
MAX_LOG_DEPTH = 8
MAX_LOG_NODES = 256
_EVENT_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PROCESS_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SAFE_LOGGER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_FIELD_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONTENT_KEYS = frozenset(
    {
        "content",
        "file_path",
        "filename",
        "image",
        "message",
        "ocr",
        "ocr_text",
        "path",
        "prompt",
        "ref_audio_path",
        "screenshot",
        "source_path",
        "text",
        "transcript",
        "uri",
        "url",
        "wav",
        "window_title",
    }
)
_SAFE_IDENTIFIER_KEYS = frozenset(
    {
        "error_code",
        "key_id",
        "secret_id",
        "session_fingerprint",
    }
)
_CREDENTIAL_MARKERS = (
    "api_key",
    "apikey",
    "access_token",
    "accesstoken",
    "authorization",
    "client_secret",
    "clientsecret",
    "cookie",
    "credential",
    "password",
    "passwd",
    "refresh_token",
    "refreshtoken",
    "secret",
    "session_id",
    "sessionid",
    "token",
)
_IDENTIFIER_FIELDS = {
    "message_id": "message_fingerprint",
    "session_id": "session_fingerprint",
    "turn_id": "turn_fingerprint",
}
_CODE_FIELDS = frozenset(
    {
        "capability",
        "environment",
        "error_code",
        "event_type",
        "input_mode",
        "model",
        "observer",
        "phase",
        "provider",
        "reason",
        "resource",
        "sink",
        "status",
        "transport",
        "version",
    }
)
_BOOLEAN_FIELDS = frozenset({"dev_api_enabled", "expression_update"})
_METRIC_FIELDS = frozenset(
    {
        "audio_queue_wait_ms",
        "first_sentence_play_ms",
        "latency_ms",
        "llm_first_segment_ms",
        "llm_first_token_ms",
        "playback_count",
        "queue_size",
        "segment_count",
        "temp_deleted",
        "temp_pending",
        "temp_rejected",
        "text_length",
        "tts_first_audio_ms",
        "tts_job_latency_ms",
        "turn_total_ms",
    }
)
_FINGERPRINT_FIELDS = frozenset({"fingerprint", *_IDENTIFIER_FIELDS.values()})
_SAFE_REDACTION_KEYS = frozenset(
    {
        *_SAFE_IDENTIFIER_KEYS,
        *_CODE_FIELDS,
        *_BOOLEAN_FIELDS,
        *_METRIC_FIELDS,
        *_FINGERPRINT_FIELDS,
        "event",
        "level",
        "logger",
        "redaction_applied",
        "schema_version",
        "timestamp",
    }
)


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _sensitive_key(value: str) -> bool:
    normalized = _normalize_key(value)
    if normalized in _SAFE_REDACTION_KEYS:
        return False
    if any(_key_contains_marker(normalized, marker) for marker in _CONTENT_KEYS):
        return True
    return any(_key_contains_marker(normalized, marker) for marker in _CREDENTIAL_MARKERS)


def _key_contains_marker(normalized: str, marker: str) -> bool:
    return (
        normalized == marker
        or normalized.startswith(f"{marker}_")
        or normalized.endswith(f"_{marker}")
        or f"_{marker}_" in normalized
    )


def _bounded_redacted_text(value: str) -> str:
    if len(value) <= MAX_LOG_VALUE_CHARS:
        return value
    truncated = value[:MAX_LOG_VALUE_CHARS]
    for prefix_length in range(len(REDACTED) - 1, 0, -1):
        prefix = REDACTED[:prefix_length]
        if truncated.endswith(prefix):
            return truncated[: MAX_LOG_VALUE_CHARS - len(REDACTED)] + REDACTED
    return truncated


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _privacy_marker_present(value: Any) -> bool:
    if isinstance(value, str):
        return value in {BINARY, REDACTED, UNSERIALIZABLE} or REDACTED in value
    if isinstance(value, Mapping):
        return any(_privacy_marker_present(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_privacy_marker_present(item) for item in value)
    return False


def _safe_metric(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return UNSERIALIZABLE
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return UNSERIALIZABLE
        return value if 0 <= value <= 1_000_000_000 else UNSERIALIZABLE
    if isinstance(value, list) and all(
        isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 1_000_000_000
        for item in value
    ):
        return value
    return UNSERIALIZABLE


class Redactor:
    """Redact keys and values while producing bounded JSON-compatible data."""

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
        (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), REDACTED),
        (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), REDACTED),
        (re.compile(r"\bAIza[A-Za-z0-9_-]{16,}\b"), REDACTED),
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
        lookahead = max((len(secret) for secret in self._known_secrets), default=0)
        result = value[: MAX_LOG_VALUE_CHARS + lookahead]
        for secret in self._known_secrets:
            result = result.replace(secret, REDACTED)
        result = _bounded_redacted_text(result)
        for pattern, replacement in self._patterns:
            result = pattern.sub(replacement, result)
        return _bounded_redacted_text(result)

    def redact(self, value: Any) -> Any:
        return self._redact(value, depth=0, active=set(), budget=[MAX_LOG_NODES])

    def _redact(
        self,
        value: Any,
        *,
        depth: int,
        active: set[int],
        budget: list[int],
    ) -> Any:
        if budget[0] <= 0 or depth > MAX_LOG_DEPTH:
            return UNSERIALIZABLE
        budget[0] -= 1
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else UNSERIALIZABLE
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, bytes | bytearray | memoryview):
            return BINARY
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in active:
                return UNSERIALIZABLE
            active.add(identity)
            try:
                result: dict[str, Any] = {}
                for key, item in value.items():
                    safe_key = self.redact_text(key) if isinstance(key, str) else "non_string_key"
                    if _sensitive_key(safe_key) or _privacy_marker_present(safe_key):
                        result["redacted_key"] = REDACTED
                    else:
                        result[safe_key] = self._redact(
                            item,
                            depth=depth + 1,
                            active=active,
                            budget=budget,
                        )
                    if budget[0] <= 0:
                        break
                return result
            except BaseException:
                return UNSERIALIZABLE
            finally:
                active.discard(identity)
        if isinstance(value, list | tuple | set | frozenset):
            identity = id(value)
            if identity in active:
                return UNSERIALIZABLE
            active.add(identity)
            try:
                items: list[Any] = []
                for item in value:
                    items.append(
                        self._redact(
                            item,
                            depth=depth + 1,
                            active=active,
                            budget=budget,
                        )
                    )
                    if budget[0] <= 0:
                        break
                return items
            except BaseException:
                return UNSERIALIZABLE
            finally:
                active.discard(identity)
        return UNSERIALIZABLE


class JsonFormatter(logging.Formatter):
    def __init__(
        self,
        redactor: Redactor,
        *,
        max_record_bytes: int = MAX_LOG_RECORD_BYTES,
    ) -> None:
        super().__init__()
        self._redactor = redactor
        self._max_record_bytes = max_record_bytes

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(UTC).isoformat()
        event = self._safe_event(record)
        try:
            payload: dict[str, Any] = {
                "schema_version": LOG_SCHEMA_VERSION,
                "timestamp": timestamp,
                "level": record.levelname if record.levelname in logging._nameToLevel else "ERROR",
                "logger": record.name if _SAFE_LOGGER.fullmatch(record.name) else "unknown",
                "event": event,
            }
            fields = getattr(record, "structured_fields", None)
            if isinstance(fields, Mapping):
                payload.update(self._allowlisted_fields(fields))
            if record.exc_info:
                payload["error_code"] = "logged_exception"
            normalized = self._redactor.redact(payload)
            serialized = json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            if len(serialized.encode("utf-8")) <= self._max_record_bytes:
                return serialized
        except BaseException:
            pass
        return json.dumps(
            {
                "schema_version": LOG_SCHEMA_VERSION,
                "timestamp": timestamp,
                "level": "ERROR",
                "logger": "megumin_companion",
                "event": "logging.record_unavailable",
                "error_code": "log_serialization_failed",
            },
            separators=(",", ":"),
        )

    def _safe_event(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except BaseException:
            return "logging.message_unavailable"
        redacted = self._redactor.redact_text(message)
        return redacted if _EVENT_CODE.fullmatch(redacted) else "logging.unstructured_message"

    def _allowlisted_fields(self, fields: Mapping[Any, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        redaction_applied = False
        for key, value in fields.items():
            if not isinstance(key, str):
                redaction_applied = True
                continue
            normalized = _normalize_key(key)
            fingerprint_field = _IDENTIFIER_FIELDS.get(normalized)
            if fingerprint_field is not None:
                if isinstance(value, str):
                    result[fingerprint_field] = _fingerprint(value)
                else:
                    redaction_applied = True
                continue
            redacted = self._redactor.redact(value)
            if normalized in _CODE_FIELDS:
                if isinstance(redacted, str) and _SAFE_FIELD_CODE.fullmatch(redacted):
                    result[normalized] = redacted
                else:
                    redaction_applied = True
                continue
            if normalized in _BOOLEAN_FIELDS:
                if isinstance(redacted, bool):
                    result[normalized] = redacted
                else:
                    redaction_applied = True
                continue
            if normalized in _METRIC_FIELDS:
                metric = _safe_metric(redacted)
                if metric != UNSERIALIZABLE:
                    result[normalized] = metric
                else:
                    redaction_applied = True
                continue
            if normalized in _FINGERPRINT_FIELDS:
                if isinstance(redacted, str) and re.fullmatch(r"[A-Fa-f0-9]{8,128}", redacted):
                    result[normalized] = redacted
                else:
                    redaction_applied = True
                continue
            if _privacy_marker_present(redacted):
                redaction_applied = True
        if redaction_applied:
            result["redaction_applied"] = REDACTED
        return result


def _handler(stream: Any, redactor: Redactor) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(redactor))
    return handler


def _process_log_path(logical_path: Path, process_name: str, process_id: int) -> Path:
    normalized = process_name.strip().casefold()
    if not _PROCESS_NAME.fullmatch(normalized):
        raise ValueError("invalid logging process name")
    return logical_path.with_name(
        f"{logical_path.stem}.{normalized}.{process_id}{logical_path.suffix}"
    )


def _windows_process_is_alive(process_id: int) -> bool:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return False
    kernel32 = loader("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, 0, process_id)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint32()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
            exit_code.value == 259
        )
    finally:
        kernel32.CloseHandle(handle)


def _process_is_alive(process_id: int) -> bool:
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_is_alive(process_id)
    try:
        os.kill(process_id, 0)
    except (OSError, ValueError):
        return False
    return True


class BoundedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """One process-owned size lineage plus cross-lineage age cleanup."""

    def __init__(
        self,
        filename: Path,
        *,
        logical_path: Path,
        max_bytes: int,
        file_count: int,
        retention_days: int,
    ) -> None:
        self._logical_path = logical_path
        self._retention_seconds = retention_days * 24 * 60 * 60
        self._cleanup_expired()
        self._active_since = self._oldest_record_time(filename)
        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=max(0, file_count - 1),
            encoding="utf-8",
        )
        self._maintenance_stop = threading.Event()
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name=f"log-retention-{os.getpid()}",
            daemon=True,
        )
        self._maintenance_thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        if not self._expire_active_if_required():
            return
        try:
            message = self.format(record)
            line = message + self.terminator
            message_bytes = len(message.encode("utf-8")) + len(os.linesep.encode("utf-8"))
            if self.maxBytes > 0:
                if self.stream is None:
                    self.stream = self._open()
                try:
                    current_bytes = Path(self.baseFilename).stat().st_size
                except OSError:
                    current_bytes = 0
                if current_bytes + message_bytes > self.maxBytes:
                    self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            self.stream.write(line)
            self.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.maxBytes <= 0:
            return False
        if self.stream is None:
            self.stream = self._open()
        try:
            current_bytes = Path(self.baseFilename).stat().st_size
        except OSError:
            current_bytes = 0
        message_bytes = len(self.format(record).encode("utf-8")) + len(os.linesep.encode("utf-8"))
        return current_bytes + message_bytes > self.maxBytes

    def doRollover(self) -> None:
        if self.backupCount > 0:
            super().doRollover()
        else:
            if self.stream:
                self.stream.close()
                self.stream = None
            try:
                Path(self.baseFilename).write_text("", encoding="utf-8")
            except OSError as exc:
                raise OSError("log_rotation_failed") from exc
            if not self.delay:
                self.stream = self._open()
        self._active_since = time.time()
        self._cleanup_expired()

    def handleError(self, record: logging.LogRecord) -> None:
        """Fail closed without reflecting filesystem paths or record contents."""

        del record

    def close(self) -> None:
        self._maintenance_stop.set()
        if self._maintenance_thread is not threading.current_thread():
            self._maintenance_thread.join(timeout=1.0)
        super().close()

    def _maintenance_loop(self) -> None:
        while True:
            active_deadline = self._active_since + self._retention_seconds
            delay = max(0.1, min(60.0, active_deadline - time.time()))
            if self._maintenance_stop.wait(delay):
                return
            self.acquire()
            try:
                self._expire_active_if_required()
                self._cleanup_expired()
            finally:
                self.release()

    def _expire_active_if_required(self) -> bool:
        now = time.time()
        if now - self._active_since < self._retention_seconds:
            return True
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
            Path(self.baseFilename).write_text("", encoding="utf-8")
            if not self.delay:
                self.stream = self._open()
            self._active_since = now
            return True
        except OSError:
            return False

    @staticmethod
    def _oldest_record_time(filename: Path) -> float:
        if not filename.exists():
            return time.time()
        try:
            with filename.open("r", encoding="utf-8") as stream:
                line = stream.readline(MAX_LOG_RECORD_BYTES + 2)
            if not line or len(line.encode("utf-8")) > MAX_LOG_RECORD_BYTES + 1:
                return 0.0
            first = json.loads(line)
            timestamp = first["timestamp"]
            if not isinstance(timestamp, str):
                return 0.0
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return 0.0
            return parsed.timestamp()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return 0.0

    def _cleanup_expired(self) -> None:
        parent = self._logical_path.parent
        if not parent.exists():
            return
        cutoff = time.time() - self._retention_seconds
        stem = re.escape(self._logical_path.stem)
        suffix = re.escape(self._logical_path.suffix)
        owned = re.compile(rf"^{stem}\.[a-z][a-z0-9_-]{{0,31}}\.(\d+){suffix}(?:\.\d+)?$")
        for candidate in parent.glob(f"{self._logical_path.stem}.*{self._logical_path.suffix}*"):
            match = owned.fullmatch(candidate.name)
            if match is None or candidate.is_symlink():
                continue
            try:
                is_archive = not candidate.name.endswith(self._logical_path.suffix)
                metadata = candidate.stat()
                content_expired = self._oldest_record_time(candidate) < cutoff
                if (
                    candidate.is_file()
                    and (metadata.st_mtime < cutoff or content_expired)
                    and (is_archive or not _process_is_alive(int(match.group(1))))
                ):
                    candidate.unlink()
            except OSError:
                continue


def _ensure_managed_log_path(settings: Settings, path: Path) -> Path:
    log_root = settings.paths.logs.absolute()
    candidate = path.absolute()
    if not candidate.is_relative_to(log_root):
        raise ValueError("log path must stay under LocalAppData logs")
    return path


def configure_logging(
    settings: Settings,
    *,
    file_path: Path | None = None,
    additional_secrets: Sequence[str] = (),
    process_name: str = "main",
) -> logging.Logger:
    """Configure one process-owned logger without touching unrelated loggers."""

    logger = logging.getLogger("megumin_companion")
    close_logging(logger)
    logger.setLevel(settings.app.log_level)
    logger.propagate = False
    redactor = Redactor((*settings.known_secret_values(), *additional_secrets))

    if settings.logging.console_enabled:
        logger.addHandler(_handler(sys.stdout, redactor))
    if settings.logging.file_enabled:
        logical_path = _ensure_managed_log_path(
            settings,
            file_path or settings.log_file_path(),
        )
        directory_security_for_current_platform().ensure_private_tree(
            settings.paths.root,
            (settings.paths.logs, logical_path.parent),
        )
        target = _process_log_path(logical_path, process_name, os.getpid())
        file_handler = BoundedRotatingFileHandler(
            target,
            logical_path=logical_path,
            max_bytes=settings.logging.max_bytes,
            file_count=settings.logging.file_count,
            retention_days=settings.logging.retention_days,
        )
        file_handler.setFormatter(
            JsonFormatter(
                redactor,
                max_record_bytes=min(
                    MAX_LOG_RECORD_BYTES,
                    settings.logging.max_bytes - 2,
                ),
            )
        )
        logger.addHandler(file_handler)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def close_logging(logger: logging.Logger) -> None:
    """Best-effort flush and bounded handler close without reflecting failures."""

    for handler in tuple(logger.handlers):
        with suppress(Exception):
            handler.flush()
        with suppress(Exception):
            handler.close()
    logger.handlers.clear()


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    logger.log(level, event, extra={"structured_fields": fields})
