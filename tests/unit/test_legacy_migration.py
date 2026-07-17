"""W02 explicit legacy data copy/verify/switch behavior."""

import json
import sqlite3
from pathlib import Path

import pytest
from app.legacy_migration import (
    LegacyMigrationError,
    migrate_legacy_data,
    preflight_legacy_data,
)
from app.paths import AppPaths


def _legacy_tree(tmp_path: Path) -> Path:
    source = tmp_path / "旧 项目" / "data"
    database = source / "private" / "companion.sqlite3"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES ('preserved')")
        connection.commit()
    finally:
        connection.close()
    model = source / "models" / "whisper" / "模型.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"deterministic-model")
    (source / "logs").mkdir()
    (source / "logs" / "old.jsonl").write_text("private legacy log", encoding="utf-8")
    (source / "cache").mkdir()
    (source / "cache" / "derived.bin").write_bytes(b"skip-cache")
    (source / "private" / "stt").mkdir()
    (source / "private" / "stt" / "recording.wav").write_bytes(b"skip-temp")
    (source / "private" / "vts-token.json").write_text("secret", encoding="utf-8")
    (source.parent / ".env").write_text("SECRET=not-migrated\n", encoding="utf-8")
    return source


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths.from_local_app_data(tmp_path / "D 盘" / ("长路径" * 12))


def test_preflight_has_fixed_allowlist_and_skip_policy(tmp_path: Path) -> None:
    source = _legacy_tree(tmp_path)

    plan = preflight_legacy_data(source, app_paths=_paths(tmp_path))

    assert plan.database_present
    assert plan.model_file_count == 1
    assert plan.model_bytes == len(b"deterministic-model")
    assert plan.secrets_require_reentry
    assert plan.logs_skipped
    assert plan.cache_skipped
    assert plan.temporary_files_skipped


def test_migration_copies_verifies_and_atomically_activates(tmp_path: Path) -> None:
    source = _legacy_tree(tmp_path)
    paths = _paths(tmp_path)

    result = migrate_legacy_data(source, app_paths=paths)

    assert result.database_migrated
    assert result.model_file_count == 1
    assert result.backup_created
    assert result.source_preserved and source.exists()
    assert result.target_activated and paths.root.exists()
    assert (source.parent / ".env").exists()
    assert not (paths.root / ".env").exists()
    assert not (paths.secrets / "vts-token.json").exists()
    assert not paths.logs.exists()
    assert not paths.cache.exists()
    assert not paths.temp.exists()
    assert (paths.models / "whisper" / "模型.bin").read_bytes() == b"deterministic-model"

    connection = sqlite3.connect(paths.state / "companion.sqlite3")
    try:
        assert connection.execute("SELECT value FROM sample").fetchone() == ("preserved",)
    finally:
        connection.close()
    assert (paths.state / "backups" / "legacy-before-w02.sqlite3").is_file()
    manifest_text = (paths.root / "migration.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["source_preserved"] is True
    assert str(tmp_path) not in manifest_text
    assert "not-migrated" not in manifest_text


@pytest.mark.parametrize("step", ["copy", "verify", "switch"])
def test_fault_at_each_stage_preserves_source_and_removes_staging(
    tmp_path: Path, step: str
) -> None:
    source = _legacy_tree(tmp_path)
    paths = _paths(tmp_path)

    def inject(active: str) -> None:
        if active == step:
            raise OSError(f"injected {step}")

    with pytest.raises(OSError, match=f"injected {step}"):
        migrate_legacy_data(source, app_paths=paths, fault_injector=inject)

    assert source.exists()
    assert (source / "private" / "companion.sqlite3").is_file()
    assert not paths.root.exists()
    assert not list(paths.root.parent.glob(f"{paths.root.name}.migration-*"))


def test_existing_target_is_never_merged_or_overwritten(tmp_path: Path) -> None:
    source = _legacy_tree(tmp_path)
    paths = _paths(tmp_path)
    paths.config.mkdir(parents=True)
    sentinel = paths.config / "settings.yaml"
    sentinel.write_text("existing", encoding="utf-8")

    with pytest.raises(LegacyMigrationError, match="不会覆盖或合并"):
        migrate_legacy_data(source, app_paths=paths)

    assert sentinel.read_text(encoding="utf-8") == "existing"
    assert source.exists()


def test_partial_copy_failure_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import legacy_migration as module

    source = _legacy_tree(tmp_path)
    paths = _paths(tmp_path)

    def partial_copy(_source_root: Path, target_root: Path, _files: tuple[Path, ...]) -> None:
        target_root.mkdir(parents=True, exist_ok=True)
        (target_root / "partial.bin").write_bytes(b"partial")
        raise OSError("injected partial copy")

    monkeypatch.setattr(module, "_copy_models", partial_copy)

    with pytest.raises(OSError, match="injected partial copy"):
        migrate_legacy_data(source, app_paths=paths)

    assert source.exists()
    assert not paths.root.exists()
    assert not list(paths.root.parent.glob(f"{paths.root.name}.migration-*"))


def test_missing_source_does_not_create_target(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    with pytest.raises(LegacyMigrationError, match="不存在或不可访问"):
        migrate_legacy_data(tmp_path / "missing-data", app_paths=paths)

    assert not paths.root.exists()


def test_corrupt_database_fails_during_read_only_preflight(tmp_path: Path) -> None:
    source = tmp_path / "data"
    database = source / "private" / "companion.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(LegacyMigrationError, match="只读校验"):
        preflight_legacy_data(source, app_paths=_paths(tmp_path))

    assert database.read_bytes() == b"not a sqlite database"
