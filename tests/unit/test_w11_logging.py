"""W11 property and fault tests for bounded, process-owned logging."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.config.logging import (
    REDACTED,
    BoundedRotatingFileHandler,
    Redactor,
    close_logging,
    configure_logging,
    log_event,
)
from app.config.settings import LoggingConfig
from app.paths import AppPaths
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st


class _ExplosiveRepresentation:
    def __str__(self) -> str:
        raise RuntimeError("W11_REPR_SENTINEL")

    def __repr__(self) -> str:
        raise RuntimeError("W11_REPR_SENTINEL")


class _ExplosiveException(Exception):
    def __str__(self) -> str:
        raise RuntimeError("W11_EXCEPTION_REPR_SENTINEL")


class _SinglePassFields(dict[str, Any]):
    calls = 0

    def items(self) -> Any:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("W11_SECOND_MAPPING_PASS_SENTINEL")
        return super().items()

    def __repr__(self) -> str:
        raise RuntimeError("W11_EXCEPTION_REPR_SENTINEL")


def _logging_settings(
    root: Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    file_count: int = 5,
    retention_days: int = 14,
) -> Settings:
    result = Settings(
        logging=LoggingConfig(
            console_enabled=False,
            file_enabled=True,
            file_path=Path("app.jsonl"),
            max_bytes=max_bytes,
            file_count=file_count,
            retention_days=retention_days,
        )
    )
    result._paths = AppPaths(root=root)
    return result


def _file_handler(logger: logging.Logger) -> logging.FileHandler:
    handlers = [item for item in logger.handlers if isinstance(item, logging.FileHandler)]
    assert len(handlers) == 1
    return handlers[0]


def _write_from_process(root_text: str, marker: str) -> str:
    settings = _logging_settings(Path(root_text), max_bytes=32 * 1024)
    logger = configure_logging(settings, process_name="worker")
    for index in range(40):
        log_event(logger, logging.INFO, "worker.heartbeat", marker=marker, queue_size=index)
    handler = _file_handler(logger)
    handler.flush()
    handler.close()
    return handler.baseFilename


_SENSITIVE_KEYS = st.sampled_from(
    (
        "api_key",
        "ACCESS-TOKEN",
        "authorization",
        "nested.password",
        "refresh_token",
        "session_id",
        "client_secret",
        "file_path",
        "transcript",
        "ocr_text",
    )
)


@hypothesis_settings(max_examples=80, deadline=None)
@given(
    key=_SENSITIVE_KEYS,
    suffix=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
        min_size=1,
        max_size=48,
    ),
    wrappers=st.lists(st.sampled_from(("mapping", "list", "tuple")), max_size=6),
)
def test_random_nested_sensitive_keys_redact_values_recursively(
    key: str,
    suffix: str,
    wrappers: list[str],
) -> None:
    sentinel = f"W11_PRIVATE_VALUE_{suffix}"
    value: Any = {key: sentinel}
    for index, wrapper in enumerate(wrappers):
        if wrapper == "mapping":
            value = {f"safe_{index}": value}
        elif wrapper == "list":
            value = [value]
        else:
            value = (value,)

    redacted = Redactor().redact(value)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert sentinel not in serialized
    assert REDACTED in serialized


@hypothesis_settings(max_examples=60, deadline=None)
@given(
    suffix=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
        min_size=12,
        max_size=48,
    ),
    unicode_label=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
        min_size=1,
        max_size=32,
    ),
)
def test_random_nested_value_patterns_and_unicode_are_safe(
    suffix: str,
    unicode_label: str,
) -> None:
    secret = f"sk-{suffix}"
    value = {
        "label": unicode_label,
        "nested": [{"ordinary": f"prefix {secret} suffix"}],
    }

    redacted = Redactor().redact(value)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert secret not in serialized
    assert REDACTED in serialized
    assert redacted["label"] == unicode_label


def test_exception_and_object_repr_are_never_called(tmp_path: Path) -> None:
    logger = configure_logging(_logging_settings(tmp_path / "LocalAppData"))

    log_event(
        logger,
        logging.ERROR,
        "serialization.failed",
        unsafe=_ExplosiveRepresentation(),
        nested=[_ExplosiveRepresentation()],
    )
    try:
        raise _ExplosiveException()
    except _ExplosiveException:
        logger.exception(_ExplosiveRepresentation())
    handler = _file_handler(logger)
    handler.flush()

    output = Path(handler.baseFilename).read_text(encoding="utf-8")
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == 2
    assert "W11_REPR_SENTINEL" not in output
    assert "W11_EXCEPTION_REPR_SENTINEL" not in output
    assert "unsafe" not in records[0]
    assert records[0]["redaction_applied"] == REDACTED
    assert records[1]["event"] == "logging.message_unavailable"
    assert records[1]["error_code"] == "logged_exception"


def test_file_handler_formats_each_record_only_once(tmp_path: Path) -> None:
    logger = configure_logging(_logging_settings(tmp_path / "LocalAppData"))
    fields = _SinglePassFields(queue_size=7)

    logger.info("single.pass", extra={"structured_fields": fields})
    handler = _file_handler(logger)
    handler.flush()
    payload = json.loads(Path(handler.baseFilename).read_text(encoding="utf-8"))

    assert fields.calls == 1
    assert payload["event"] == "single.pass"
    assert payload["queue_size"] == 7


def test_handler_close_still_runs_when_flush_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("w11-close-fault")
    handler = logging.NullHandler()
    closed = False

    def fail_flush() -> None:
        raise OSError("W11_FLUSH_PATH_SENTINEL")

    def observe_close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(handler, "flush", fail_flush)
    monkeypatch.setattr(handler, "close", observe_close)
    logger.handlers = [handler]

    close_logging(logger)

    assert closed
    assert logger.handlers == []


def test_log_field_allowlist_drops_body_paths_and_hashes_identifiers(tmp_path: Path) -> None:
    logger = configure_logging(_logging_settings(tmp_path / "LocalAppData"))
    body = "W11_ARBITRARY_BODY_SENTINEL"
    user_path = "C:\\Users\\private-user\\draft.txt"

    log_event(
        logger,
        logging.ERROR,
        "allowlist.sample",
        arbitrary_body=body,
        path=user_path,
        error_code="provider_timeout",
        queue_size=3,
        turn_id="turn-private-value",
        session_id="session-private-value",
    )
    handler = _file_handler(logger)
    handler.flush()
    output = Path(handler.baseFilename).read_text(encoding="utf-8")
    payload = json.loads(output)

    assert body not in output
    assert user_path not in output
    assert "turn-private-value" not in output
    assert "session-private-value" not in output
    assert set(payload) == {
        "schema_version",
        "timestamp",
        "level",
        "logger",
        "event",
        "error_code",
        "queue_size",
        "turn_fingerprint",
        "session_fingerprint",
    }


def test_allowlisted_field_types_fail_closed_individually(tmp_path: Path) -> None:
    logger = configure_logging(_logging_settings(tmp_path / "LocalAppData"))
    logger.info(
        "allowlist.types",
        extra={
            "structured_fields": {
                7: "non-string-key",
                "dev_api_enabled": True,
                "expression_update": "not-a-bool",
                "reason": "stable_reason",
                "resource": "C:/private/path",
                "latency_ms": None,
                "turn_total_ms": 42,
                "tts_job_latency_ms": [1, 2, 3],
                "bad_count": True,
                "session_fingerprint": "abcdef1234567890",
                "bad_fingerprint": "not-hex",
                "C:\\Users\\private-user\\secret_count": 7,
                "private_path_fingerprint": "abcdef1234567890",
            }
        },
    )
    handler = _file_handler(logger)
    handler.flush()
    payload = json.loads(Path(handler.baseFilename).read_text(encoding="utf-8"))

    assert payload["dev_api_enabled"] is True
    assert payload["reason"] == "stable_reason"
    assert payload["latency_ms"] is None
    assert payload["turn_total_ms"] == 42
    assert payload["tts_job_latency_ms"] == [1, 2, 3]
    assert payload["session_fingerprint"] == "abcdef1234567890"
    assert payload["redaction_applied"] == REDACTED
    assert "expression_update" not in payload
    assert "resource" not in payload
    assert "bad_count" not in payload
    assert "bad_fingerprint" not in payload
    assert "private_user" not in json.dumps(payload)
    assert "private_path_fingerprint" not in payload


def test_known_secret_crossing_string_limit_is_fully_redacted() -> None:
    secret = "W11-BOUNDARY-SECRET-" + "x" * 256
    prefix = "p" * 4_090

    redacted = Redactor((secret,)).redact_text(prefix + secret + "tail")

    assert secret not in redacted
    assert not redacted.endswith(secret[:6])
    assert REDACTED in redacted


def test_recursive_cycles_and_non_string_keys_are_serialized_without_repr() -> None:
    key = _ExplosiveRepresentation()
    value: dict[Any, Any] = {key: "safe"}
    value["cycle"] = value

    serialized = json.dumps(Redactor().redact(value))

    assert "W11_REPR_SENTINEL" not in serialized
    assert "[UNSERIALIZABLE]" in serialized


def test_sensitive_mapping_keys_and_values_are_both_replaced_recursively() -> None:
    user_path_key = "C:\\Users\\W11-AUDIT-USER\\draft_file_path"
    token_key = "sk-W11KeySentinel123456"

    serialized = json.dumps(
        Redactor().redact(
            {
                user_path_key: "W11_PRIVATE_VALUE",
                "nested": {token_key: "ordinary"},
            }
        ),
        ensure_ascii=False,
    )

    assert user_path_key not in serialized
    assert "W11-AUDIT-USER" not in serialized
    assert token_key not in serialized
    assert "W11_PRIVATE_VALUE" not in serialized
    assert "redacted_key" in serialized
    assert REDACTED in serialized


def test_size_rotation_and_age_retention_are_both_enforced(tmp_path: Path) -> None:
    root = tmp_path / "LocalAppData" / "MeguminCompanion"
    settings = _logging_settings(root, max_bytes=700, file_count=3, retention_days=1)
    settings.paths.logs.mkdir(parents=True)
    expired = settings.paths.logs / "app.worker.999999.jsonl"
    expired.write_text('{"event":"expired"}\n', encoding="utf-8")
    old = 946_684_800
    os.utime(expired, (old, old))

    logger = configure_logging(settings, process_name="main")
    for index in range(60):
        log_event(
            logger,
            logging.INFO,
            "rotation.sample",
            queue_size=index,
            unicode_label="雪山🔥" * 12,
        )
    handler = _file_handler(logger)
    handler.flush()

    owned_files = sorted(settings.paths.logs.glob("app.main.*.jsonl*"))
    assert 1 < len(owned_files) <= 3
    assert all(item.stat().st_size <= 700 for item in owned_files)
    assert not expired.exists()


def test_single_file_configuration_truncates_instead_of_growing_unbounded(
    tmp_path: Path,
) -> None:
    settings = _logging_settings(
        tmp_path / "LocalAppData" / "MeguminCompanion",
        max_bytes=700,
        file_count=1,
    )
    logger = configure_logging(settings, process_name="main")

    for index in range(60):
        log_event(logger, logging.INFO, "rotation.single", queue_size=index, label="雪" * 80)
    handler = _file_handler(logger)
    handler.flush()
    files = list(settings.paths.logs.glob("app.main.*.jsonl*"))

    assert len(files) == 1
    assert files[0].stat().st_size <= 700


def test_active_lineage_drops_records_older_than_retention_before_next_emit(
    tmp_path: Path,
) -> None:
    settings = _logging_settings(
        tmp_path / "LocalAppData" / "MeguminCompanion",
        retention_days=1,
    )
    settings.paths.logs.mkdir(parents=True)
    active = settings.paths.logs / f"app.main.{os.getpid()}.jsonl"
    active.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timestamp": "2000-01-01T00:00:00+00:00",
                "level": "INFO",
                "logger": "megumin_companion",
                "event": "expired.active_record",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    logger = configure_logging(settings, process_name="main")
    log_event(logger, logging.INFO, "retention.current_record")
    handler = _file_handler(logger)
    handler.flush()
    output = active.read_text(encoding="utf-8")

    assert "expired.active_record" not in output
    assert "retention.current_record" in output


def test_dead_lineage_uses_record_age_even_when_file_mtime_is_recent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _logging_settings(
        tmp_path / "LocalAppData" / "MeguminCompanion",
        retention_days=1,
    )
    settings.paths.logs.mkdir(parents=True)
    expired = settings.paths.logs / "app.worker.424242.jsonl"
    expired.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timestamp": "2000-01-01T00:00:00+00:00",
                "level": "INFO",
                "logger": "megumin_companion",
                "event": "expired.dead_process_record",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.logging._process_is_alive", lambda _pid: False)

    logger = configure_logging(settings, process_name="main")

    assert not expired.exists()
    for handler in tuple(logger.handlers):
        handler.close()
    logger.handlers.clear()


def test_concurrent_processes_never_share_a_log_file(tmp_path: Path) -> None:
    root = tmp_path / "LocalAppData" / "MeguminCompanion"
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        futures = [
            executor.submit(_write_from_process, str(root), marker) for marker in ("alpha", "beta")
        ]
        paths = [Path(item.result(timeout=30)) for item in futures]

    assert len(set(paths)) == 2
    assert all(item.parent == root / "logs" for item in paths)
    for path in paths:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 40


def test_default_log_file_is_process_owned_under_local_app_data(tmp_path: Path) -> None:
    settings = _logging_settings(tmp_path / "LocalAppData" / "MeguminCompanion")
    logger = configure_logging(settings, process_name="main")

    handler = _file_handler(logger)
    path = Path(handler.baseFilename)

    assert path.parent == settings.paths.logs
    assert path.name.startswith("app.main.")
    assert path.suffix == ".jsonl"
    assert isinstance(handler, BoundedRotatingFileHandler)
    assert handler._maintenance_thread.is_alive()
    handler.close()
    assert not handler._maintenance_thread.is_alive()


def test_logging_limits_can_only_be_tightened() -> None:
    with pytest.raises(ValueError):
        LoggingConfig(max_bytes=10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        LoggingConfig(file_count=6)
    with pytest.raises(ValueError):
        LoggingConfig(retention_days=15)


def test_explicit_log_override_cannot_leave_local_app_data(tmp_path: Path) -> None:
    settings = _logging_settings(tmp_path / "LocalAppData" / "MeguminCompanion")

    with pytest.raises(ValueError, match="LocalAppData"):
        configure_logging(settings, file_path=tmp_path / "outside.jsonl")
