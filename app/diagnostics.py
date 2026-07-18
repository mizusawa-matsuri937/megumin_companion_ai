"""Explicit, path-free diagnostic ZIP export with fail-closed privacy scans."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import stat
import time
import zipfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.paths import AppPaths
from app.windows_security import directory_security_for_current_platform

DIAGNOSTIC_MANIFEST_SCHEMA_VERSION = 1
MAX_DIAGNOSTIC_FILES = 10
MAX_DIAGNOSTIC_DIRECTORY_ENTRIES = 1_024
MAX_DIAGNOSTIC_SOURCE_BYTES = 10 * 1024 * 1024 + 64 * 1024
MAX_DIAGNOSTIC_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTIC_LINE_BYTES = 64 * 1024
MAX_CRASH_REPORTS = 5
MAX_CRASH_RETENTION_DAYS = 14
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SAFE_SOURCE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_LOGGER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_SAFE_FINGERPRINT = re.compile(r"^[A-Fa-f0-9]{8,128}$")
_CRASH_NAME = re.compile(r"^crash\.[a-z][a-z0-9_-]{0,31}\.\d+\.\d+\.json$")
_WINDOWS_PATH = re.compile(r"(?i)(?:^|[\s\"'])(?:[a-z]:|\\\\)[\\/]+[^\r\n\"']+")
_HOME_PATH = re.compile(r"(?:^|[\s\"'])(?:/Users/|/home/)[^\r\n\"']+")
_STRONG_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{16,}\b"),
)
_DIAGNOSTIC_METRIC_FIELDS = frozenset(
    {
        "audio_queue_wait_ms",
        "first_sentence_play_ms",
        "latency_ms",
        "llm_first_segment_ms",
        "llm_first_token_ms",
        "playback_count",
        "queue_size",
        "segment_count",
        "tts_first_audio_ms",
        "tts_job_latency_ms",
        "turn_total_ms",
    }
)
_SOURCE_METRIC_FIELDS = frozenset(
    {
        *_DIAGNOSTIC_METRIC_FIELDS,
        "temp_deleted",
        "temp_pending",
        "temp_rejected",
        "text_length",
    }
)
_FINGERPRINT_FIELDS = frozenset(
    {"fingerprint", "message_fingerprint", "session_fingerprint", "turn_fingerprint"}
)
_SOURCE_CODE_FIELDS = frozenset(
    {
        "capability",
        "environment",
        "error_code",
        "event",
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
_SOURCE_BOOLEAN_FIELDS = frozenset({"dev_api_enabled", "expression_update"})
_ALLOWED_FIELDS = frozenset(
    {
        "capability",
        "error_code",
        "event",
        *_FINGERPRINT_FIELDS,
        *_DIAGNOSTIC_METRIC_FIELDS,
        "level",
        "logger",
        "schema_version",
        "status",
        "timestamp",
        "version",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        *_ALLOWED_FIELDS,
        *_SOURCE_CODE_FIELDS,
        *_SOURCE_BOOLEAN_FIELDS,
        *_SOURCE_METRIC_FIELDS,
        "redaction_applied",
    }
)
_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
FaultInjector = Callable[[str], None]


class DiagnosticExportError(RuntimeError):
    """Stable diagnostic export failure without source names or contents."""


@dataclass(frozen=True, slots=True)
class DiagnosticExportResult:
    archive_created: bool
    manifest_schema_version: int
    member_count: int
    archive_fingerprint: str


@dataclass(frozen=True, slots=True)
class _BundleEntry:
    name: str
    payload: bytes
    source_payload: bytes


def _validate_code(value: str) -> str:
    if not _SAFE_CODE.fullmatch(value):
        raise DiagnosticExportError("diagnostic_field_invalid")
    return value


def _is_reparse_or_symlink(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _safe_read(path: Path, expected: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DiagnosticExportError("diagnostic_source_invalid") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != expected.st_size
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
            or opened.st_mtime_ns != expected.st_mtime_ns
        ):
            raise DiagnosticExportError("diagnostic_source_invalid")
        chunks: list[bytes] = []
        remaining = expected.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        finished = os.fstat(descriptor)
        if (
            len(result) != expected.st_size
            or finished.st_size != opened.st_size
            or finished.st_dev != opened.st_dev
            or finished.st_ino != opened.st_ino
            or finished.st_mtime_ns != opened.st_mtime_ns
        ):
            raise DiagnosticExportError("diagnostic_source_invalid")
        return result
    finally:
        os.close(descriptor)


def _safe_timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise DiagnosticExportError("diagnostic_field_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiagnosticExportError("diagnostic_field_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DiagnosticExportError("diagnostic_field_invalid")
    return value


def _safe_metric(value: Any) -> int | float | list[int] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise DiagnosticExportError("diagnostic_field_invalid")
    if isinstance(value, int):
        numeric: int | float = int(value)
    elif isinstance(value, float):
        numeric = float(value)
    elif isinstance(value, list) and len(value) <= 256:
        if all(
            isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 1_000_000_000
            for item in value
        ):
            return value
        raise DiagnosticExportError("diagnostic_field_invalid")
    else:
        raise DiagnosticExportError("diagnostic_field_invalid")
    if (
        numeric < 0
        or numeric > 1_000_000_000
        or (isinstance(numeric, float) and not math.isfinite(numeric))
    ):
        raise DiagnosticExportError("diagnostic_field_invalid")
    return numeric


def _validate_ignored_source_field(key: str, value: Any) -> None:
    if key in _SOURCE_CODE_FIELDS:
        if not isinstance(value, str) or not _SAFE_SOURCE_CODE.fullmatch(value):
            raise DiagnosticExportError("diagnostic_field_invalid")
        return
    if key in _SOURCE_BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise DiagnosticExportError("diagnostic_field_invalid")
        return
    if key in _SOURCE_METRIC_FIELDS:
        _safe_metric(value)
        return
    if key == "redaction_applied" and value == "[REDACTED]":
        return
    raise DiagnosticExportError("diagnostic_field_invalid")


def _sanitize_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DiagnosticExportError("diagnostic_source_invalid")
    if not value.keys() <= _SOURCE_FIELDS:
        raise DiagnosticExportError("diagnostic_source_invalid")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _ALLOWED_FIELDS:
            _validate_ignored_source_field(key, item)
            continue
        if key == "schema_version":
            if item != 1 or isinstance(item, bool):
                raise DiagnosticExportError("diagnostic_field_invalid")
            result[key] = 1
        elif key == "timestamp":
            result[key] = _safe_timestamp(item)
        elif key == "level":
            if not isinstance(item, str) or item not in _LEVELS:
                raise DiagnosticExportError("diagnostic_field_invalid")
            result[key] = item
        elif key == "logger":
            if not isinstance(item, str) or not _SAFE_LOGGER.fullmatch(item):
                raise DiagnosticExportError("diagnostic_field_invalid")
            result[key] = item
        elif key in {"capability", "error_code", "event", "status"}:
            if not isinstance(item, str):
                raise DiagnosticExportError("diagnostic_field_invalid")
            result[key] = _validate_code(item)
        elif key in _FINGERPRINT_FIELDS:
            if not isinstance(item, str) or not _SAFE_FINGERPRINT.fullmatch(item):
                raise DiagnosticExportError("diagnostic_field_invalid")
            result[key] = item
        elif key == "version":
            if not isinstance(item, str) or not _SAFE_VERSION.fullmatch(item):
                raise DiagnosticExportError("diagnostic_field_invalid")
            result[key] = item
        elif key in _DIAGNOSTIC_METRIC_FIELDS:
            result[key] = _safe_metric(item)
        else:  # pragma: no cover - all allowlisted fields are handled above
            raise DiagnosticExportError("diagnostic_field_invalid")
    required = {"schema_version", "timestamp", "level", "logger", "event"}
    if not required <= result.keys():
        raise DiagnosticExportError("diagnostic_source_invalid")
    return result


def _sanitize_json_lines(raw: bytes, *, single_record: bool) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiagnosticExportError("diagnostic_source_invalid") from exc
    lines = text.splitlines()
    if single_record and len(lines) != 1:
        raise DiagnosticExportError("diagnostic_source_invalid")
    output: list[bytes] = []
    for line in lines:
        encoded = line.encode("utf-8")
        if not encoded or len(encoded) > MAX_DIAGNOSTIC_LINE_BYTES:
            raise DiagnosticExportError("diagnostic_source_invalid")
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise DiagnosticExportError("diagnostic_source_invalid") from exc
        sanitized = _sanitize_record(parsed)
        output.append(
            json.dumps(
                sanitized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    if not output:
        raise DiagnosticExportError("diagnostic_source_invalid")
    return b"".join(output)


class DiagnosticExporter:
    """Export only sanitized logs/crash summaries after a manifest-first scan."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        logical_log_path: Path | None = None,
        known_secrets: Sequence[str] = (),
        forbidden_values: Sequence[str] = (),
    ) -> None:
        self._paths = paths
        logical = logical_log_path or paths.logs / "app.jsonl"
        resolved = logical.resolve(strict=False)
        log_root = paths.logs.resolve(strict=False)
        if logical.suffix.casefold() != ".jsonl" or not resolved.is_relative_to(log_root):
            raise DiagnosticExportError("diagnostic_log_path_invalid")
        self._log_directory = logical.parent
        self._log_name = re.compile(
            rf"^{re.escape(logical.stem)}\.[a-z][a-z0-9_-]{{0,31}}\.\d+"
            rf"{re.escape(logical.suffix)}(?:\.\d+)?$"
        )
        self._forbidden = tuple(value for value in (*known_secrets, *forbidden_values) if value)

    def export(
        self,
        destination: Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> DiagnosticExportResult:
        target = destination.expanduser()
        if target.suffix.casefold() != ".zip":
            raise DiagnosticExportError("diagnostic_destination_invalid")
        if target.exists():
            raise DiagnosticExportError("diagnostic_destination_exists")
        directory_security_for_current_platform().ensure_private_tree(
            self._paths.root,
            (self._paths.logs, self._paths.temp, self._log_directory),
        )
        staging_root = self._paths.temp / "diagnostics"
        staging_root.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_hex(8)
        staging = staging_root / f"export-{nonce}"
        partial = target.with_name(f"{target.name}.{nonce}.partial")
        staging.mkdir()
        try:
            entries = self._collect_entries()
            manifest = self._manifest(entries)
            (staging / "manifest.json").write_bytes(manifest)
            self._fault(fault_injector, "manifest_created")
            self._privacy_scan(manifest)
            for entry in entries:
                self._raw_privacy_scan(entry.source_payload)
                self._privacy_scan(entry.payload)
            self._fault(fault_injector, "privacy_scan_completed")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(
                partial,
                "x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                archive.writestr("manifest.json", manifest)
                for entry in entries:
                    archive.writestr(entry.name, entry.payload)
            self._verify_archive(partial, manifest, entries)
            archive_fingerprint = sha256(partial.read_bytes()).hexdigest()
            self._fault(fault_injector, "partial_archive_written")
            os.replace(partial, target)
            return DiagnosticExportResult(
                archive_created=True,
                manifest_schema_version=DIAGNOSTIC_MANIFEST_SCHEMA_VERSION,
                member_count=1 + len(entries),
                archive_fingerprint=archive_fingerprint,
            )
        finally:
            try:
                partial.unlink(missing_ok=True)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                with suppress(OSError):
                    staging_root.rmdir()

    @staticmethod
    def _fault(injector: FaultInjector | None, phase: str) -> None:
        if injector is not None:
            injector(phase)

    def _collect_entries(self) -> tuple[_BundleEntry, ...]:
        candidates: list[tuple[int, Path, bool]] = []
        entries_seen = 0
        directories = tuple(dict.fromkeys((self._log_directory, self._paths.logs)))
        for directory in directories:
            try:
                iterator = os.scandir(directory)
            except OSError as exc:
                raise DiagnosticExportError("diagnostic_source_invalid") from exc
            with iterator:
                for item in iterator:
                    entries_seen += 1
                    if entries_seen > MAX_DIAGNOSTIC_DIRECTORY_ENTRIES:
                        raise DiagnosticExportError("diagnostic_source_limit")
                    is_log = (
                        directory == self._log_directory
                        and self._log_name.fullmatch(item.name) is not None
                    )
                    is_crash = (
                        directory == self._paths.logs
                        and _CRASH_NAME.fullmatch(item.name) is not None
                    )
                    if not is_log and not is_crash:
                        continue
                    path = Path(item.path)
                    try:
                        metadata = path.lstat()
                        if _is_reparse_or_symlink(path) or not stat.S_ISREG(metadata.st_mode):
                            raise DiagnosticExportError("diagnostic_source_invalid")
                    except OSError as exc:
                        raise DiagnosticExportError("diagnostic_source_invalid") from exc
                    if metadata.st_size <= 0 or metadata.st_size > MAX_DIAGNOSTIC_SOURCE_BYTES:
                        raise DiagnosticExportError("diagnostic_source_limit")
                    candidates.append((metadata.st_mtime_ns, path, is_crash))
        candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        selected = candidates[:MAX_DIAGNOSTIC_FILES]
        total = 0
        log_index = 0
        crash_index = 0
        entries: list[_BundleEntry] = []
        for _modified, path, is_crash in selected:
            metadata = path.lstat()
            total += metadata.st_size
            if total > MAX_DIAGNOSTIC_TOTAL_BYTES:
                raise DiagnosticExportError("diagnostic_source_limit")
            raw = _safe_read(path, metadata)
            payload = _sanitize_json_lines(raw, single_record=is_crash)
            if is_crash:
                crash_index += 1
                name = f"crash/crash-{crash_index:04d}.json"
            else:
                log_index += 1
                name = f"logs/log-{log_index:04d}.jsonl"
            entries.append(_BundleEntry(name=name, payload=payload, source_payload=raw))
        return tuple(entries)

    @staticmethod
    def _manifest(entries: Sequence[_BundleEntry]) -> bytes:
        payload = {
            "schema_version": DIAGNOSTIC_MANIFEST_SCHEMA_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "scan_policy": "w11_allowlist_v1",
            "default_exclusions": ["database", "screenshot", "wav", "user_path"],
            "members": [
                {
                    "name": item.name,
                    "size_bytes": len(item.payload),
                    "sha256": sha256(item.payload).hexdigest(),
                }
                for item in entries
            ],
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _raw_privacy_scan(self, payload: bytes) -> None:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DiagnosticExportError("diagnostic_privacy_scan_failed") from exc
        forbidden = (*self._forbidden, str(self._paths.root), str(Path.home()))
        for value in forbidden:
            if value and (value in text or json.dumps(value)[1:-1] in text):
                raise DiagnosticExportError("diagnostic_privacy_scan_failed")
        if _WINDOWS_PATH.search(text) or _HOME_PATH.search(text):
            raise DiagnosticExportError("diagnostic_privacy_scan_failed")
        if any(pattern.search(text) for pattern in _STRONG_SECRET_PATTERNS):
            raise DiagnosticExportError("diagnostic_privacy_scan_failed")

    def _privacy_scan(self, payload: bytes) -> None:
        self._raw_privacy_scan(payload)

    def _verify_archive(
        self,
        partial: Path,
        manifest: bytes,
        entries: Sequence[_BundleEntry],
    ) -> None:
        expected = ("manifest.json", *(entry.name for entry in entries))
        try:
            with zipfile.ZipFile(partial) as archive:
                names = tuple(archive.namelist())
                if names != expected:
                    raise DiagnosticExportError("diagnostic_archive_invalid")
                expected_payloads = (manifest, *(entry.payload for entry in entries))
                for name, payload in zip(names, expected_payloads, strict=True):
                    if archive.read(name) != payload:
                        raise DiagnosticExportError("diagnostic_archive_invalid")
                    self._privacy_scan(payload)
        except (OSError, zipfile.BadZipFile) as exc:
            raise DiagnosticExportError("diagnostic_archive_invalid") from exc


def write_crash_report(
    paths: AppPaths,
    *,
    error_code: str,
    exception: BaseException | None = None,
    process_name: str = "main",
    file_count: int = MAX_CRASH_REPORTS,
    retention_days: int = MAX_CRASH_RETENTION_DAYS,
) -> Path:
    """Write a content-free crash summary; exception text and traceback are never inspected."""

    del exception
    _validate_code(error_code)
    normalized_process = process_name.strip().casefold()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", normalized_process):
        raise DiagnosticExportError("diagnostic_field_invalid")
    if not 1 <= file_count <= MAX_CRASH_REPORTS:
        raise DiagnosticExportError("diagnostic_field_invalid")
    if not 1 <= retention_days <= MAX_CRASH_RETENTION_DAYS:
        raise DiagnosticExportError("diagnostic_field_invalid")
    directory_security_for_current_platform().ensure_private_tree(
        paths.root,
        (paths.logs, paths.temp),
    )
    timestamp = datetime.now(UTC)
    payload = {
        "schema_version": 1,
        "timestamp": timestamp.isoformat(),
        "level": "ERROR",
        "logger": "megumin_companion",
        "event": "application.crashed",
        "error_code": error_code,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    target = paths.logs / (f"crash.{normalized_process}.{os.getpid()}.{time.time_ns()}.json")
    partial = target.with_suffix(".json.partial")
    try:
        with partial.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, target)
        _cleanup_crash_reports(
            paths.logs,
            file_count=file_count,
            retention_days=retention_days,
        )
        return target
    finally:
        partial.unlink(missing_ok=True)


def _cleanup_crash_reports(log_root: Path, *, file_count: int, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 24 * 60 * 60
    retained: list[tuple[int, Path]] = []
    try:
        iterator = os.scandir(log_root)
    except OSError:
        return
    with iterator:
        for item in iterator:
            if _CRASH_NAME.fullmatch(item.name) is None:
                continue
            candidate = Path(item.path)
            try:
                metadata = candidate.lstat()
                if _is_reparse_or_symlink(candidate) or not stat.S_ISREG(metadata.st_mode):
                    continue
                if metadata.st_mtime < cutoff:
                    candidate.unlink()
                    continue
                retained.append((metadata.st_mtime_ns, candidate))
            except OSError:
                continue
    retained.sort(key=lambda value: (value[0], value[1].name), reverse=True)
    for _modified, candidate in retained[file_count:]:
        with suppress(OSError):
            candidate.unlink()
