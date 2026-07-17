"""Crash-safe registry and fail-closed scavenger for sensitive temp assets."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from uuid import uuid4

from app.paths import AppPaths
from app.windows_security import (
    DirectorySecurity,
    ReparsePointError,
    WindowsSecurityError,
    assert_no_reparse_points,
    directory_security_for_current_platform,
    is_reparse_point,
)

TEMP_REGISTRY_FORMAT_VERSION = 1
_MAX_ENTRIES = 10_000
_MAX_REGISTRY_BYTES = 2 * 1024 * 1024
_MAX_DELETE_DEPTH = 32
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_ASSET_ID = re.compile(r"^tmp_[0-9a-f]{32}$")
_SAFE_WAV = re.compile(r"^[0-9]{4}-[0-9a-f]{20}\.wav$")
_SAFE_PART = re.compile(r"^\.[0-9]{4}-[0-9a-f]{20}\.wav\.[0-9a-f]{32}\.part$")
_SAFE_STT_DIRECTORY = re.compile(r"^companion-(?:recording|stt)-[0-9a-f]{32}$")


class TempAssetKind(StrEnum):
    tts_part = "tts_part"
    tts_wav = "tts_wav"
    mock_wav = "mock_wav"
    recording_directory = "recording_directory"
    stt_directory = "stt_directory"


class TempDeleteStatus(StrEnum):
    deleted = "deleted"
    missing = "missing"
    pending = "pending"
    rejected = "rejected"


class TempRegistryError(RuntimeError):
    """Registry error whose text never includes a user path or asset content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TempAssetEntry:
    asset_id: str
    relative_path: str
    kind: TempAssetKind
    created_at: float
    retry_count: int = 0
    next_retry_at: float = 0.0


@dataclass(frozen=True, slots=True)
class TempDeleteResult:
    asset_id: str
    status: TempDeleteStatus
    retry_count: int = 0
    next_retry_at: float = 0.0


@dataclass(frozen=True, slots=True)
class TempScavengeReport:
    deleted: int
    missing: int
    pending: int
    rejected: int
    discovered: int


_LOCKS_GUARD = threading.Lock()
_REGISTRY_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.absolute()).casefold()
    with _LOCKS_GUARD:
        return _REGISTRY_LOCKS.setdefault(key, threading.RLock())


class TempAssetRegistry:
    """Persist ownership before temp creation and remove it only after deletion."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        clock: Callable[[], float] = time.time,
        minimum_scavenge_age_seconds: float = 300.0,
        retry_base_seconds: float = 0.25,
        retry_max_seconds: float = 30.0,
        directory_security: DirectorySecurity | None = None,
    ) -> None:
        if (
            minimum_scavenge_age_seconds < 0.0
            or retry_base_seconds <= 0.0
            or retry_max_seconds < retry_base_seconds
        ):
            raise ValueError("invalid temp registry timing bounds")
        self._paths = paths
        self._registry_path = paths.state / "temp-assets.json"
        self._clock = clock
        self._minimum_age = minimum_scavenge_age_seconds
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._directory_security = directory_security or directory_security_for_current_platform()
        self._lock = _lock_for(self._registry_path)

    @property
    def registry_path(self) -> Path:
        return self._registry_path

    def prepare(self) -> None:
        self._directory_security.ensure_private_tree(
            self._paths.root,
            (self._paths.state, self._paths.temp),
        )
        assert_no_reparse_points(self._paths.root, self._paths.state)
        assert_no_reparse_points(self._paths.root, self._paths.temp)
        with self._lock:
            self._load_entries()

    def register(
        self,
        path: Path,
        kind: TempAssetKind,
        *,
        asset_id: str | None = None,
    ) -> TempAssetEntry:
        self.prepare()
        relative = self._relative(path)
        self._validate_name(relative, kind)
        try:
            self._directory_security.ensure_private_tree(
                self._paths.root,
                (path.parent,),
            )
            assert_no_reparse_points(self._paths.root, path.parent)
        except WindowsSecurityError as exc:
            raise TempRegistryError("temp_registry_reparse_rejected") from exc
        selected_id = asset_id or f"tmp_{uuid4().hex}"
        if not _ASSET_ID.fullmatch(selected_id):
            raise TempRegistryError("temp_registry_invalid_asset_id")
        with self._lock:
            entries = self._load_entries()
            for existing in entries.values():
                if existing.relative_path == relative.as_posix():
                    return existing
            if len(entries) >= _MAX_ENTRIES or selected_id in entries:
                raise TempRegistryError("temp_registry_capacity_or_id_conflict")
            entry = TempAssetEntry(
                asset_id=selected_id,
                relative_path=relative.as_posix(),
                kind=kind,
                created_at=self._clock(),
            )
            entries[selected_id] = entry
            self._write_entries(entries)
            return entry

    def mark_moved(
        self,
        asset_id: str,
        path: Path,
        *,
        kind: TempAssetKind | None = None,
    ) -> TempAssetEntry:
        relative = self._relative(path)
        try:
            assert_no_reparse_points(self._paths.root, path)
        except ReparsePointError as exc:
            raise TempRegistryError("temp_registry_reparse_rejected") from exc
        with self._lock:
            entries = self._load_entries()
            entry = entries.get(asset_id)
            if entry is None:
                raise TempRegistryError("temp_registry_unknown_asset")
            selected_kind = kind or entry.kind
            self._validate_name(relative, selected_kind)
            updated = replace(
                entry,
                relative_path=relative.as_posix(),
                kind=selected_kind,
            )
            entries[asset_id] = updated
            self._write_entries(entries)
            return updated

    def delete(self, asset_id: str, *, ignore_retry_deadline: bool = False) -> TempDeleteResult:
        with self._lock:
            entries = self._load_entries()
            entry = entries.get(asset_id)
            if entry is None:
                return TempDeleteResult(asset_id, TempDeleteStatus.missing)
            result = self._delete_entry(
                entry,
                entries,
                ignore_retry_deadline=ignore_retry_deadline,
            )
            self._write_entries(entries)
            return result

    def delete_path(self, path: Path, *, ignore_retry_deadline: bool = False) -> TempDeleteResult:
        relative = self._relative(path).as_posix()
        with self._lock:
            entries = self._load_entries()
            entry = next(
                (item for item in entries.values() if item.relative_path == relative),
                None,
            )
            if entry is None:
                return TempDeleteResult("tmp_unregistered", TempDeleteStatus.missing)
            result = self._delete_entry(
                entry,
                entries,
                ignore_retry_deadline=ignore_retry_deadline,
            )
            self._write_entries(entries)
            return result

    def entries(self) -> tuple[TempAssetEntry, ...]:
        with self._lock:
            return tuple(self._load_entries().values())

    def scavenge(self) -> TempScavengeReport:
        self.prepare()
        now = self._clock()
        counts = {
            TempDeleteStatus.deleted: 0,
            TempDeleteStatus.missing: 0,
            TempDeleteStatus.pending: 0,
            TempDeleteStatus.rejected: 0,
        }
        discovered = 0
        with self._lock:
            entries = self._load_entries()
            for entry in tuple(entries.values()):
                if now - entry.created_at < self._minimum_age:
                    counts[TempDeleteStatus.pending] += 1
                    continue
                result = self._delete_entry(entry, entries)
                counts[result.status] += 1

            registered = {entry.relative_path for entry in entries.values()}
            for path, kind in self._discover_candidates():
                relative = self._relative(path)
                if relative.as_posix() in registered:
                    continue
                try:
                    age = now - path.lstat().st_mtime
                except OSError:
                    continue
                if age < self._minimum_age:
                    continue
                discovered += 1
                entry = TempAssetEntry(
                    asset_id=f"tmp_{uuid4().hex}",
                    relative_path=relative.as_posix(),
                    kind=kind,
                    created_at=path.lstat().st_mtime,
                )
                entries[entry.asset_id] = entry
                result = self._delete_entry(entry, entries, ignore_retry_deadline=True)
                counts[result.status] += 1
            self._write_entries(entries)
        return TempScavengeReport(
            deleted=counts[TempDeleteStatus.deleted],
            missing=counts[TempDeleteStatus.missing],
            pending=counts[TempDeleteStatus.pending],
            rejected=counts[TempDeleteStatus.rejected],
            discovered=discovered,
        )

    def _delete_entry(
        self,
        entry: TempAssetEntry,
        entries: dict[str, TempAssetEntry],
        *,
        ignore_retry_deadline: bool = False,
    ) -> TempDeleteResult:
        now = self._clock()
        if not ignore_retry_deadline and entry.next_retry_at > now:
            return TempDeleteResult(
                entry.asset_id,
                TempDeleteStatus.pending,
                entry.retry_count,
                entry.next_retry_at,
            )
        try:
            relative = self._parse_relative(entry.relative_path)
            self._validate_name(relative, entry.kind)
            path = self._paths.temp.joinpath(*relative.parts)
            assert_no_reparse_points(self._paths.root, path)
            existed = path.exists()
            _delete_without_following_reparse(path)
        except (ReparsePointError, TempRegistryError):
            updated = self._schedule_retry(entry, now)
            entries[entry.asset_id] = updated
            return TempDeleteResult(
                entry.asset_id,
                TempDeleteStatus.rejected,
                updated.retry_count,
                updated.next_retry_at,
            )
        except OSError:
            updated = self._schedule_retry(entry, now)
            entries[entry.asset_id] = updated
            return TempDeleteResult(
                entry.asset_id,
                TempDeleteStatus.pending,
                updated.retry_count,
                updated.next_retry_at,
            )
        entries.pop(entry.asset_id, None)
        return TempDeleteResult(
            entry.asset_id,
            TempDeleteStatus.deleted if existed else TempDeleteStatus.missing,
        )

    def _schedule_retry(self, entry: TempAssetEntry, now: float) -> TempAssetEntry:
        retry_count = min(entry.retry_count + 1, 31)
        delay = min(self._retry_max, self._retry_base * (2 ** (retry_count - 1)))
        return replace(
            entry,
            retry_count=retry_count,
            next_retry_at=now + delay,
        )

    def _relative(self, path: Path) -> PurePosixPath:
        try:
            relative = path.absolute().relative_to(self._paths.temp.absolute())
        except ValueError as exc:
            raise TempRegistryError("temp_registry_path_escape") from exc
        return self._parse_relative(relative.as_posix())

    def _parse_relative(self, value: str) -> PurePosixPath:
        candidate = PurePosixPath(value)
        if (
            candidate.is_absolute()
            or not candidate.parts
            or ".." in candidate.parts
            or len(value) > 1024
            or any(not _SAFE_COMPONENT.fullmatch(part) for part in candidate.parts)
        ):
            raise TempRegistryError("temp_registry_invalid_relative_path")
        return candidate

    def _validate_name(self, relative: PurePosixPath, kind: TempAssetKind) -> None:
        name = relative.name
        if kind in {TempAssetKind.recording_directory, TempAssetKind.stt_directory}:
            if not _SAFE_STT_DIRECTORY.fullmatch(name):
                raise TempRegistryError("temp_registry_invalid_asset_name")
            expected_prefix = (
                "companion-recording-"
                if kind is TempAssetKind.recording_directory
                else "companion-stt-"
            )
            if not name.startswith(expected_prefix):
                raise TempRegistryError("temp_registry_invalid_asset_name")
            return
        if kind in {TempAssetKind.tts_wav, TempAssetKind.mock_wav}:
            if relative.parts[0] != "audio" or not _SAFE_WAV.fullmatch(name):
                raise TempRegistryError("temp_registry_invalid_asset_name")
            return
        if kind is TempAssetKind.tts_part:
            if relative.parts[0] != "audio" or not _SAFE_PART.fullmatch(name):
                raise TempRegistryError("temp_registry_invalid_asset_name")
            return
        raise TempRegistryError("temp_registry_invalid_asset_kind")

    def _load_entries(self) -> dict[str, TempAssetEntry]:
        try:
            try:
                registry_stat = self._registry_path.lstat()
            except FileNotFoundError:
                return {}
            assert_no_reparse_points(self._paths.root, self._registry_path)
            if not stat.S_ISREG(registry_stat.st_mode):
                raise TempRegistryError("temp_registry_corrupt")
            if registry_stat.st_size > _MAX_REGISTRY_BYTES:
                raise TempRegistryError("temp_registry_oversized")
            payload = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except TempRegistryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TempRegistryError("temp_registry_corrupt") from exc
        if not isinstance(payload, dict) or set(payload) != {"entries", "format_version"}:
            raise TempRegistryError("temp_registry_corrupt")
        if payload.get("format_version") != TEMP_REGISTRY_FORMAT_VERSION:
            raise TempRegistryError("temp_registry_future_version")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or len(raw_entries) > _MAX_ENTRIES:
            raise TempRegistryError("temp_registry_corrupt")
        entries: dict[str, TempAssetEntry] = {}
        paths: set[str] = set()
        for raw in raw_entries:
            entry = self._parse_entry(raw)
            if entry.asset_id in entries or entry.relative_path in paths:
                raise TempRegistryError("temp_registry_duplicate_entry")
            entries[entry.asset_id] = entry
            paths.add(entry.relative_path)
        return entries

    def _parse_entry(self, raw: object) -> TempAssetEntry:
        if not isinstance(raw, dict) or set(raw) != {
            "asset_id",
            "created_at",
            "kind",
            "next_retry_at",
            "relative_path",
            "retry_count",
        }:
            raise TempRegistryError("temp_registry_corrupt")
        try:
            asset_id = raw["asset_id"]
            relative_path = raw["relative_path"]
            kind = TempAssetKind(raw["kind"])
            created_at = float(raw["created_at"])
            retry_count = int(raw["retry_count"])
            next_retry_at = float(raw["next_retry_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TempRegistryError("temp_registry_corrupt") from exc
        if (
            not isinstance(asset_id, str)
            or not _ASSET_ID.fullmatch(asset_id)
            or not isinstance(relative_path, str)
            or isinstance(raw["retry_count"], bool)
            or retry_count < 0
            or retry_count > 31
            or not math.isfinite(created_at)
            or not math.isfinite(next_retry_at)
            or created_at < 0.0
            or next_retry_at < 0.0
        ):
            raise TempRegistryError("temp_registry_corrupt")
        relative = self._parse_relative(relative_path)
        self._validate_name(relative, kind)
        return TempAssetEntry(
            asset_id=asset_id,
            relative_path=relative.as_posix(),
            kind=kind,
            created_at=created_at,
            retry_count=retry_count,
            next_retry_at=next_retry_at,
        )

    def _write_entries(self, entries: dict[str, TempAssetEntry]) -> None:
        try:
            assert_no_reparse_points(self._paths.root, self._registry_path.parent)
        except ReparsePointError as exc:
            raise TempRegistryError("temp_registry_reparse_rejected") from exc
        payload = json.dumps(
            {
                "entries": [
                    {**asdict(entry), "kind": entry.kind.value}
                    for entry in sorted(entries.values(), key=lambda item: item.asset_id)
                ],
                "format_version": TEMP_REGISTRY_FORMAT_VERSION,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        temporary = self._registry_path.with_name(f".{self._registry_path.name}.{uuid4().hex}.part")
        try:
            with temporary.open("x", encoding="ascii", newline="\n") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._registry_path)
        except OSError as exc:
            raise TempRegistryError("temp_registry_write_failed") from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _discover_candidates(self) -> Iterable[tuple[Path, TempAssetKind]]:
        yield from _walk_stt_candidates(self._paths.temp)
        audio_root = self._paths.temp / "audio"
        yield from _walk_audio_candidates(audio_root)


def _walk_stt_candidates(root: Path) -> Iterable[tuple[Path, TempAssetKind]]:
    pending: list[tuple[Path, int]] = [(root, 0)]
    seen = 0
    while pending and seen < _MAX_ENTRIES:
        directory, depth = pending.pop()
        if depth > 8:
            continue
        try:
            if is_reparse_point(directory):
                continue
            children = tuple(directory.iterdir())
        except OSError:
            continue
        for child in children:
            seen += 1
            if seen > _MAX_ENTRIES:
                return
            if _SAFE_STT_DIRECTORY.fullmatch(child.name):
                kind = (
                    TempAssetKind.recording_directory
                    if child.name.startswith("companion-recording-")
                    else TempAssetKind.stt_directory
                )
                yield child, kind
                continue
            try:
                if (
                    not is_reparse_point(child)
                    and child.is_dir()
                    and _SAFE_COMPONENT.fullmatch(child.name)
                ):
                    pending.append((child, depth + 1))
            except OSError:
                continue


def _walk_audio_candidates(root: Path) -> Iterable[tuple[Path, TempAssetKind]]:
    pending: list[tuple[Path, int]] = [(root, 0)]
    seen = 0
    while pending and seen < _MAX_ENTRIES:
        directory, depth = pending.pop()
        if depth > 8:
            continue
        try:
            if is_reparse_point(directory):
                continue
            children = tuple(directory.iterdir())
        except OSError:
            continue
        for child in children:
            seen += 1
            if seen > _MAX_ENTRIES:
                return
            try:
                if is_reparse_point(child):
                    continue
                if child.is_dir():
                    if _SAFE_COMPONENT.fullmatch(child.name):
                        pending.append((child, depth + 1))
                    continue
            except OSError:
                continue
            if _SAFE_PART.fullmatch(child.name):
                yield child, TempAssetKind.tts_part
            elif _SAFE_WAV.fullmatch(child.name):
                kind = (
                    TempAssetKind.mock_wav
                    if "mock" in child.relative_to(root).parts
                    else TempAssetKind.tts_wav
                )
                yield child, kind


def _delete_without_following_reparse(path: Path) -> None:
    pending: list[tuple[Path, int]] = [(path, 0)]
    files: list[Path] = []
    directories: list[Path] = []
    seen = 0
    # Complete a bounded, no-follow preflight before removing anything.  A
    # reparse point anywhere in an STT directory therefore rejects the whole
    # deletion rather than partially clearing it first.
    while pending:
        candidate, depth = pending.pop()
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if is_reparse_point(candidate):
            raise ReparsePointError("temp deletion rejected a reparse point")
        seen += 1
        if seen > _MAX_ENTRIES or depth > _MAX_DELETE_DEPTH:
            raise OSError("temp deletion bounds exceeded")
        if stat.S_ISDIR(candidate_stat.st_mode):
            directories.append(candidate)
            pending.extend((child, depth + 1) for child in candidate.iterdir())
        else:
            files.append(candidate)

    for candidate in files:
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if is_reparse_point(candidate) or stat.S_ISDIR(candidate_stat.st_mode):
            raise ReparsePointError("temp file changed during deletion")
        candidate.unlink()
    for candidate in reversed(directories):
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if is_reparse_point(candidate) or not stat.S_ISDIR(candidate_stat.st_mode):
            raise ReparsePointError("temp directory changed during deletion")
        candidate.rmdir()
