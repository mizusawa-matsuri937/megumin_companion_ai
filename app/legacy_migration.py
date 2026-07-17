"""Explicit copy-verify-switch migration from the former repository ``data/`` tree."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.paths import AppPaths

MAX_MODEL_FILES = 10_000
MAX_MODEL_BYTES = 32 * 1024 * 1024 * 1024
MigrationStep = Literal["copy", "verify", "switch"]
FaultInjector = Callable[[MigrationStep], None]


class LegacyMigrationError(RuntimeError):
    """Raised when migration cannot finish without risking old or active data."""


@dataclass(frozen=True, slots=True)
class LegacyMigrationPlan:
    database_present: bool
    model_file_count: int
    model_bytes: int
    secrets_require_reentry: bool
    logs_skipped: bool
    cache_skipped: bool
    temporary_files_skipped: bool


@dataclass(frozen=True, slots=True)
class LegacyMigrationResult:
    database_migrated: bool
    model_file_count: int
    backup_created: bool
    source_preserved: bool
    target_activated: bool
    secrets_require_reentry: bool


def _path_fingerprint(path: Path) -> str:
    return hashlib.sha256(os.fsencode(str(path))).hexdigest()[:16]


def _safe_source(source: Path | str, paths: AppPaths) -> Path:
    candidate = Path(source).expanduser()
    if candidate.is_symlink():
        raise LegacyMigrationError("迁移源目录不能是符号链接。")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LegacyMigrationError("迁移源目录不存在或不可访问。") from exc
    if not resolved.is_dir():
        raise LegacyMigrationError("迁移源必须是旧 data 目录。")
    target = paths.root.resolve(strict=False)
    if resolved == target or resolved in target.parents or target in resolved.parents:
        raise LegacyMigrationError("迁移源与目标目录不能重叠。")
    return resolved


def _model_inventory(models: Path) -> tuple[tuple[Path, ...], int]:
    if not models.exists():
        return (), 0
    if models.is_symlink() or not models.is_dir():
        raise LegacyMigrationError("旧模型目录类型不安全。")
    files: list[Path] = []
    total_bytes = 0
    for item in models.rglob("*"):
        if item.is_symlink():
            raise LegacyMigrationError("旧模型目录包含符号链接，已拒绝迁移。")
        if not item.is_file():
            continue
        files.append(item)
        if len(files) > MAX_MODEL_FILES:
            raise LegacyMigrationError("旧模型文件数量超过迁移上限。")
        try:
            total_bytes += item.stat().st_size
        except OSError as exc:
            raise LegacyMigrationError("无法读取旧模型文件元数据。") from exc
        if total_bytes > MAX_MODEL_BYTES:
            raise LegacyMigrationError("旧模型总大小超过迁移上限。")
    return tuple(files), total_bytes


def preflight_legacy_data(
    source: Path | str, *, app_paths: AppPaths | None = None
) -> LegacyMigrationPlan:
    """Inspect only the explicit source and return the fixed allowlist plan."""

    paths = app_paths or AppPaths.discover()
    root = _safe_source(source, paths)
    database = root / "private" / "companion.sqlite3"
    if database.is_symlink():
        raise LegacyMigrationError("旧数据库不能是符号链接。")
    database_present = database.is_file()
    if database_present:
        _verify_database(database)
    model_files, model_bytes = _model_inventory(root / "models")
    if not database_present and not model_files:
        raise LegacyMigrationError("显式源目录中没有可迁移的数据库或模型。")
    return LegacyMigrationPlan(
        database_present=database_present,
        model_file_count=len(model_files),
        model_bytes=model_bytes,
        secrets_require_reentry=(root / ".env").exists()
        or (root.parent / ".env").exists()
        or (root / "private" / "vts-token.json").exists(),
        logs_skipped=(root / "logs").exists(),
        cache_skipped=(root / "cache").exists(),
        temporary_files_skipped=(root / "private" / "stt").exists() or (root / "temp").exists(),
    )


def _verify_database(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise LegacyMigrationError("旧数据库无法只读校验。") from exc
    if row is None or row[0] != "ok":
        raise LegacyMigrationError("旧数据库完整性校验失败。")


def _checkpoint_and_backup(source: Path, target: Path, backup: Path) -> None:
    try:
        checkpoint = sqlite3.connect(source, timeout=2.0)
        try:
            checkpoint.execute("PRAGMA busy_timeout = 2000")
            checkpoint.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        finally:
            checkpoint.close()

        read_connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        write_connection = sqlite3.connect(target)
        try:
            read_connection.backup(write_connection)
        finally:
            write_connection.close()
            read_connection.close()
        shutil.copy2(target, backup)
    except (OSError, sqlite3.Error) as exc:
        raise LegacyMigrationError("旧数据库 checkpoint/backup 失败。") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise LegacyMigrationError("迁移文件哈希校验失败。") from exc
    return digest.hexdigest()


def _copy_models(source_root: Path, target_root: Path, files: tuple[Path, ...]) -> None:
    for source in files:
        relative = source.relative_to(source_root)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            raise LegacyMigrationError("旧模型复制失败。") from exc


def _verify_models(source_root: Path, target_root: Path, files: tuple[Path, ...]) -> None:
    for source in files:
        target = target_root / source.relative_to(source_root)
        if not target.is_file() or _sha256(source) != _sha256(target):
            raise LegacyMigrationError("旧模型复制校验失败。")


def _cleanup_staging(staging: Path, paths: AppPaths) -> None:
    expected_parent = paths.root.parent.resolve(strict=False)
    resolved = staging.resolve(strict=False)
    if resolved.parent != expected_parent or not resolved.name.startswith(
        f"{paths.root.name}.migration-"
    ):
        raise LegacyMigrationError("拒绝清理边界外的迁移 staging。")
    try:
        shutil.rmtree(resolved, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LegacyMigrationError("迁移失败且 staging 清理未完成。") from exc


def migrate_legacy_data(
    source: Path | str,
    *,
    app_paths: AppPaths | None = None,
    fault_injector: FaultInjector | None = None,
) -> LegacyMigrationResult:
    """Copy the fixed allowlist into a sibling staging tree and atomically activate it.

    Existing LocalAppData is never merged or overwritten.  This intentionally
    narrow rule makes interruption rollback deterministic and leaves the old
    source available for the user to inspect and delete manually.
    """

    paths = app_paths or AppPaths.discover()
    source_root = _safe_source(source, paths)
    plan = preflight_legacy_data(source_root, app_paths=paths)
    if paths.root.exists():
        raise LegacyMigrationError("目标用户数据目录已存在；不会覆盖或合并。")

    model_root = source_root / "models"
    model_files, _ = _model_inventory(model_root)
    migration_id = uuid4().hex
    staging = paths.migration_staging_root(migration_id)
    if staging.exists():
        raise LegacyMigrationError("迁移 staging 已存在。")
    try:
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        if fault_injector is not None:
            fault_injector("copy")

        staged_paths = AppPaths(root=staging)
        backup_created = False
        if plan.database_present:
            staged_paths.state.mkdir(parents=True)
            backups = staged_paths.state / "backups"
            backups.mkdir()
            _checkpoint_and_backup(
                source_root / "private" / "companion.sqlite3",
                staged_paths.state / "companion.sqlite3",
                backups / "legacy-before-w02.sqlite3",
            )
            backup_created = True
        if model_files:
            staged_paths.models.mkdir(parents=True)
            _copy_models(model_root, staged_paths.models, model_files)

        if fault_injector is not None:
            fault_injector("verify")
        if plan.database_present:
            _verify_database(staged_paths.state / "companion.sqlite3")
            _verify_database(staged_paths.state / "backups" / "legacy-before-w02.sqlite3")
        _verify_models(model_root, staged_paths.models, model_files)

        manifest = {
            "schema_version": 1,
            "source_fingerprint": _path_fingerprint(source_root),
            "plan": asdict(plan),
            "source_preserved": True,
        }
        (staging / "migration.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if fault_injector is not None:
            fault_injector("switch")
        if paths.root.exists():
            raise LegacyMigrationError("目标目录在迁移期间被创建；不会覆盖。")
        os.replace(staging, paths.root)
    except BaseException:
        if staging.exists():
            _cleanup_staging(staging, paths)
        raise

    return LegacyMigrationResult(
        database_migrated=plan.database_present,
        model_file_count=plan.model_file_count,
        backup_created=backup_created,
        source_preserved=True,
        target_activated=True,
        secrets_require_reentry=plan.secrets_require_reentry,
    )
