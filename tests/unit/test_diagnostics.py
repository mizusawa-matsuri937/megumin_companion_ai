"""W11 fail-closed diagnostic bundle and crash report tests."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import cli
from app.config import Settings
from app.config.logging import REDACTED, configure_logging, log_event
from app.config.settings import LoggingConfig
from app.diagnostics import (
    DiagnosticExporter,
    DiagnosticExportError,
    write_crash_report,
)
from app.paths import AppPaths
from app.windows_security import WindowsSecurityError


class _ExplosiveCrash(Exception):
    def __str__(self) -> str:
        raise RuntimeError("W11_CRASH_REPR_SENTINEL")

    def __repr__(self) -> str:
        raise RuntimeError("W11_CRASH_REPR_SENTINEL")


def _paths(tmp_path: Path) -> AppPaths:
    paths = AppPaths(root=tmp_path / "LocalAppData" / "MeguminCompanion")
    paths.logs.mkdir(parents=True)
    paths.temp.mkdir(parents=True)
    return paths


def _write_safe_log(paths: AppPaths) -> Path:
    path = paths.logs / "app.main.1234.jsonl"
    records = [
        {
            "schema_version": 1,
            "timestamp": "2026-07-18T00:00:00+00:00",
            "level": "INFO",
            "logger": "megumin_companion",
            "event": "application.started",
            "version": "0.1.0",
        },
        {
            "schema_version": 1,
            "timestamp": "2026-07-18T00:01:00+00:00",
            "level": "ERROR",
            "logger": "megumin_companion",
            "event": "turn.failed",
            "error_code": "provider_timeout",
            "latency_ms": 150,
        },
    ]
    path.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in records),
        encoding="utf-8",
    )
    return path


def test_bundle_manifest_is_first_path_free_and_matches_zip_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    _write_safe_log(paths)
    paths.state.mkdir(parents=True)
    (paths.state / "companion.sqlite3").write_bytes(b"W11_DATABASE_SENTINEL")
    (paths.logs / "screen.png").write_bytes(b"W11_SCREENSHOT_SENTINEL")
    (paths.logs / "voice.wav").write_bytes(b"W11_WAV_SENTINEL")
    report = write_crash_report(
        paths,
        error_code="application_runtime_failure",
        exception=_ExplosiveCrash("W11_CRASH_BODY_SENTINEL"),
    )
    destination = tmp_path / "exports" / "diagnostics.zip"
    phases: list[str] = []
    original_scan = DiagnosticExporter._raw_privacy_scan

    def observe_scan(self: DiagnosticExporter, payload: bytes) -> None:
        phases.append("privacy_scan")
        original_scan(self, payload)

    monkeypatch.setattr(DiagnosticExporter, "_raw_privacy_scan", observe_scan)

    result = DiagnosticExporter(paths).export(destination, fault_injector=phases.append)

    assert result.archive_created
    assert result.manifest_schema_version == 1
    assert report.exists()
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        assert names[0] == "manifest.json"
        assert all(".." not in name and not name.startswith(("/", "\\")) for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert [item["name"] for item in manifest["members"]] == names[1:]
        for item in manifest["members"]:
            payload = archive.read(item["name"])
            assert item["size_bytes"] == len(payload)
            assert item["sha256"] == hashlib.sha256(payload).hexdigest()
        combined = b"".join(archive.read(name) for name in names)
    assert phases[0] == "manifest_created"
    assert "privacy_scan" in phases[1 : phases.index("privacy_scan_completed")]
    assert phases[-1] == "partial_archive_written"
    assert result.archive_fingerprint == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert str(paths.root).encode() not in combined
    assert b"W11_DATABASE_SENTINEL" not in combined
    assert b"W11_SCREENSHOT_SENTINEL" not in combined
    assert b"W11_WAV_SENTINEL" not in combined
    assert b"W11_CRASH_BODY_SENTINEL" not in combined
    assert b"W11_CRASH_REPR_SENTINEL" not in combined


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json\n",
        b'{"event":"application.started","note":"sk-W11secret123456"}\n',
        b'{"event":"application.started","message":"W11_BODY_SENTINEL"}\n',
        b'{"event":"application.started","path":"C:\\\\Users\\\\private\\\\file"}\n',
        b"\xff\xfe\x00",
    ],
)
def test_malicious_diagnostic_log_aborts_without_a_partial_zip(
    tmp_path: Path,
    payload: bytes,
) -> None:
    paths = _paths(tmp_path)
    (paths.logs / "app.main.1234.jsonl").write_bytes(payload)
    destination = tmp_path / "diagnostics.zip"

    with pytest.raises(DiagnosticExportError):
        DiagnosticExporter(paths, forbidden_values=("W11_BODY_SENTINEL",)).export(destination)

    assert not destination.exists()
    assert not list(destination.parent.glob("*.partial"))
    staging = paths.temp / "diagnostics"
    assert not staging.exists() or not any(staging.iterdir())


def test_matching_non_file_source_is_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    (paths.logs / "app.main.1234.jsonl").mkdir()

    with pytest.raises(DiagnosticExportError, match="diagnostic_source_invalid"):
        DiagnosticExporter(paths).export(tmp_path / "diagnostics.zip")


def test_reparse_candidate_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    _write_safe_log(paths)
    monkeypatch.setattr("app.diagnostics._is_reparse_or_symlink", lambda _path: True)

    with pytest.raises(DiagnosticExportError, match="diagnostic_source_invalid"):
        DiagnosticExporter(paths).export(tmp_path / "diagnostics.zip")


def test_source_change_during_read_fails_closed_and_cleans_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    _write_safe_log(paths)
    destination = tmp_path / "diagnostics.zip"
    original_fstat = os.fstat
    calls = 0

    def changed_fstat(descriptor: int) -> os.stat_result | SimpleNamespace:
        nonlocal calls
        calls += 1
        metadata = original_fstat(descriptor)
        if calls == 1:
            return metadata
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_size=metadata.st_size,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_mtime_ns=metadata.st_mtime_ns + 1,
        )

    monkeypatch.setattr("app.diagnostics.os.fstat", changed_fstat)

    with pytest.raises(DiagnosticExportError, match="diagnostic_source_invalid"):
        DiagnosticExporter(paths).export(destination)

    assert not destination.exists()


def test_known_secret_and_body_sentinel_scan_fail_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    secret = "provider_timeout"
    body = "application.started"
    (paths.logs / "app.main.1234.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timestamp": "2026-07-18T00:00:00+00:00",
                "level": "INFO",
                "logger": "megumin_companion",
                "event": body,
                "status": secret,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / "diagnostics.zip"

    with pytest.raises(DiagnosticExportError, match="diagnostic_privacy_scan_failed"):
        DiagnosticExporter(
            paths,
            known_secrets=(secret,),
            forbidden_values=(body,),
        ).export(destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("message", "W11_BODY_SENTINEL"),
        ("path", "C:\\Users\\private-user\\file.txt"),
        ("note", "unapproved diagnostic metadata"),
    ],
)
def test_unallowlisted_source_fields_fail_closed_instead_of_being_silently_dropped(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    paths = _paths(tmp_path)
    record = {
        "schema_version": 1,
        "timestamp": "2026-07-18T00:00:00+00:00",
        "level": "INFO",
        "logger": "megumin_companion",
        "event": "application.started",
        key: value,
    }
    (paths.logs / "app.main.1234.jsonl").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / "diagnostics.zip"

    with pytest.raises(DiagnosticExportError, match="diagnostic_source_invalid"):
        DiagnosticExporter(paths).export(destination)

    assert not destination.exists()


def test_source_only_allowlist_fields_are_validated_then_omitted_from_bundle(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    record = {
        "schema_version": 1,
        "timestamp": "2026-07-18T00:00:00+00:00",
        "level": "INFO",
        "logger": "megumin_companion",
        "event": "application.started",
        "environment": "test",
        "dev_api_enabled": True,
        "temp_deleted": 2,
        "redaction_applied": REDACTED,
        "status": "ready",
        "capability": "core",
        "version": "0.1.0",
        "llm_first_token_ms": None,
        "tts_job_latency_ms": [1, 2, 3],
        "turn_fingerprint": "abcdef1234567890",
    }
    (paths.logs / "app.main.1234.jsonl").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / "diagnostics.zip"

    DiagnosticExporter(paths).export(destination)

    with zipfile.ZipFile(destination) as archive:
        bundled = json.loads(archive.read("logs/log-0001.jsonl"))
    assert bundled["llm_first_token_ms"] is None
    assert bundled["tts_job_latency_ms"] == [1, 2, 3]
    assert bundled["turn_fingerprint"] == "abcdef1234567890"
    assert bundled["capability"] == "core"
    assert bundled["status"] == "ready"
    assert bundled["version"] == "0.1.0"
    assert (
        not {
            "environment",
            "dev_api_enabled",
            "temp_deleted",
            "redaction_applied",
        }
        & bundled.keys()
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("timestamp", "2026-07-18T00:00:00"),
        ("level", "PRIVATE"),
        ("logger", "C:/private/logger"),
        ("event", "unsafe event text"),
        ("environment", "C:/private/environment"),
        ("dev_api_enabled", "true"),
        ("latency_ms", True),
        ("latency_ms", -1),
        ("latency_ms", float("inf")),
        ("tts_job_latency_ms", [1, True]),
        ("turn_fingerprint", "not-a-fingerprint"),
        ("redaction_applied", "not-redacted"),
    ],
)
def test_invalid_allowlisted_source_values_fail_closed(
    tmp_path: Path,
    key: str,
    value: object,
) -> None:
    paths = _paths(tmp_path)
    record = {
        "schema_version": 1,
        "timestamp": "2026-07-18T00:00:00+00:00",
        "level": "INFO",
        "logger": "megumin_companion",
        "event": "application.started",
        key: value,
    }
    (paths.logs / "app.main.1234.jsonl").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DiagnosticExportError, match="diagnostic_field_invalid"):
        DiagnosticExporter(paths).export(tmp_path / "diagnostics.zip")


def test_real_logger_output_exports_without_secret_body_or_user_path(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    secret = "sk-W11RealLoggerSecret123456"
    body = "W11_REAL_LOGGER_BODY_SENTINEL"
    user_path = "C:\\Users\\private-user\\draft.txt"
    settings = Settings(
        logging=LoggingConfig(
            console_enabled=False,
            file_enabled=True,
            file_path=Path("app.jsonl"),
        )
    )
    settings._paths = paths
    logger = configure_logging(settings, additional_secrets=(secret,))
    log_event(
        logger,
        30,
        "diagnostic.integration",
        accidental_secret=secret,
        arbitrary_body=body,
        path=user_path,
        error_code="provider_timeout",
    )
    for handler in tuple(logger.handlers):
        handler.flush()
        handler.close()
    logger.handlers.clear()
    destination = tmp_path / "diagnostics.zip"

    DiagnosticExporter(
        paths,
        known_secrets=(secret,),
        forbidden_values=(body, user_path),
    ).export(destination)

    with zipfile.ZipFile(destination) as archive:
        combined = b"".join(archive.read(name) for name in archive.namelist())
    assert secret.encode() not in combined
    assert body.encode() not in combined
    assert user_path.encode() not in combined


@pytest.mark.parametrize(
    "failure_phase",
    ["manifest_created", "privacy_scan_completed", "partial_archive_written"],
)
def test_every_export_failure_cleans_staging_and_destination(
    tmp_path: Path,
    failure_phase: str,
) -> None:
    paths = _paths(tmp_path)
    _write_safe_log(paths)
    destination = tmp_path / "exports" / "diagnostics.zip"

    def fail(phase: str) -> None:
        if phase == failure_phase:
            raise RuntimeError("synthetic export fault")

    with pytest.raises(RuntimeError, match="synthetic export fault"):
        DiagnosticExporter(paths).export(destination, fault_injector=fail)

    assert not destination.exists()
    assert not list(destination.parent.glob("*.partial")) if destination.parent.exists() else True
    staging = paths.temp / "diagnostics"
    assert not staging.exists() or not any(staging.iterdir())


def test_crash_report_contains_only_allowlisted_codes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    report = write_crash_report(
        paths,
        error_code="application_runtime_failure",
        exception=_ExplosiveCrash("W11_CRASH_PRIVATE_BODY"),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))

    assert set(payload) == {
        "schema_version",
        "timestamp",
        "level",
        "logger",
        "event",
        "error_code",
    }
    assert payload["error_code"] == "application_runtime_failure"
    serialized = json.dumps(payload)
    assert "W11_CRASH_PRIVATE_BODY" not in serialized
    assert "W11_CRASH_REPR_SENTINEL" not in serialized
    assert str(tmp_path) not in serialized


def test_custom_managed_log_name_is_exported_without_source_name_or_path(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    logical = paths.logs / "nested" / "custom.jsonl"
    logical.parent.mkdir()
    source = logical.parent / "custom.main.1234.jsonl"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timestamp": "2026-07-18T00:00:00+00:00",
                "level": "INFO",
                "logger": "megumin_companion",
                "event": "custom.log",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / "custom-diagnostics.zip"

    DiagnosticExporter(paths, logical_log_path=logical).export(destination)

    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        combined = b"".join(archive.read(name) for name in names)
    assert names == ["manifest.json", "logs/log-0001.jsonl"]
    assert b"custom.main.1234" not in combined
    assert str(paths.root).encode() not in combined


def test_crash_reports_apply_count_and_age_retention(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    expired = paths.logs / "crash.main.999999.1.json"
    expired.write_text("{}", encoding="utf-8")
    os.utime(expired, (946_684_800, 946_684_800))

    for _index in range(7):
        write_crash_report(
            paths,
            error_code="application_runtime_failure",
            file_count=5,
            retention_days=14,
        )

    reports = list(paths.logs.glob("crash.*.json"))
    assert len(reports) == 5
    assert not expired.exists()


def test_cli_requires_explicit_destination_and_prints_only_path_free_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    _write_safe_log(paths)
    settings = Settings(logging=LoggingConfig(console_enabled=False, file_enabled=False))
    settings._paths = paths
    monkeypatch.setattr(cli, "_load_production_settings_for_secret_action", lambda: settings)
    destination = tmp_path / "exports" / "diagnostics.zip"

    assert cli.main(["--export-diagnostics", str(destination)]) == 0

    output = capsys.readouterr().out
    assert destination.exists()
    assert '"archive_created": true' in output
    assert '"manifest_schema_version": 1' in output
    assert str(tmp_path) not in output


def test_cli_diagnostic_security_failure_does_not_reflect_exception_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    settings = Settings(logging=LoggingConfig(console_enabled=False, file_enabled=False))
    settings._paths = paths
    monkeypatch.setattr(cli, "_load_production_settings_for_secret_action", lambda: settings)

    def fail_export(
        _self: DiagnosticExporter,
        _destination: Path,
        *,
        fault_injector: object | None = None,
    ) -> None:
        del fault_injector
        raise WindowsSecurityError(f"unsafe path: {tmp_path}")

    monkeypatch.setattr(DiagnosticExporter, "export", fail_export)

    assert cli.main(["--export-diagnostics", str(tmp_path / "diagnostics.zip")]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "diagnostic_export_error: io_failed"
    assert str(tmp_path) not in captured.err
