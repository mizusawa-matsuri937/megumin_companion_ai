"""Atomic, schema-aware per-user settings persistence."""

import os
from pathlib import Path

import pytest
import yaml
from app.config import (
    ConfigurationError,
    load_settings,
    patch_user_settings,
    read_user_settings,
    upgrade_user_settings,
    write_user_settings,
)
from app.paths import AppPaths


def paths_for(tmp_path: Path) -> AppPaths:
    return AppPaths.from_local_app_data(tmp_path / "Local")


def test_user_settings_write_is_atomic_and_keeps_one_backup(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)

    first = write_user_settings(
        {
            "schema_version": 1,
            "server": {"port": 8800},
            "logging": {"file_path": Path("custom.jsonl")},
        },
        app_paths=paths,
    )
    first_content = paths.settings.read_bytes()
    second = write_user_settings({"schema_version": 1, "server": {"port": 8900}}, app_paths=paths)

    assert first.changed and not first.backup_created
    assert second.changed and second.backup_created
    assert yaml.safe_load(paths.settings.read_text(encoding="utf-8"))["server"]["port"] == 8900
    assert paths.settings_backup.read_bytes() == first_content
    assert not list(paths.config.glob(".settings-*.tmp"))


def test_identical_user_settings_write_is_noop(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    overrides = {"schema_version": 1, "memory": {"history_retention_days": 14}}
    write_user_settings(overrides, app_paths=paths)

    result = write_user_settings(overrides, app_paths=paths)

    assert not result.changed
    assert not paths.settings_backup.exists()


def test_patch_user_settings_does_not_persist_development_environment_overrides(
    tmp_path: Path,
) -> None:
    paths = paths_for(tmp_path)
    write_user_settings(
        {"schema_version": 1, "llm": {"model": "persisted-model"}},
        app_paths=paths,
    )
    effective = load_settings(
        app_paths=paths,
        environ={"MEGUMIN_LLM_MODEL": "temporary-development-model"},
    )
    assert effective.llm.model == "temporary-development-model"

    patch_user_settings({"desktop": {"startup_enabled": True}}, app_paths=paths)

    layer = read_user_settings(app_paths=paths)
    assert layer["llm"]["model"] == "persisted-model"
    assert layer["desktop"]["startup_enabled"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 1, "llm": {"api_key": "plaintext"}},
        {"schema_version": 1, "provider": {"access-token": "plaintext"}},
        {"schema_version": 1, "nested": [{"password": "plaintext"}]},
    ],
)
def test_plaintext_secret_fields_are_rejected_before_write(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    paths = paths_for(tmp_path)

    with pytest.raises(ConfigurationError, match="明文 secret"):
        write_user_settings(overrides, app_paths=paths)

    assert not paths.root.exists()


def test_failed_atomic_switch_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = paths_for(tmp_path)
    write_user_settings({"schema_version": 1, "server": {"port": 8800}}, app_paths=paths)
    original = paths.settings.read_bytes()
    real_replace = os.replace

    def fail_target_switch(source: Path | str, target: Path | str) -> None:
        if Path(target) == paths.settings:
            raise PermissionError("injected switch failure")
        real_replace(source, target)

    monkeypatch.setattr("app.config.user_settings.os.replace", fail_target_switch)

    with pytest.raises(ConfigurationError, match="原文件保持不变"):
        write_user_settings({"schema_version": 1, "server": {"port": 8900}}, app_paths=paths)

    assert paths.settings.read_bytes() == original
    assert paths.settings_backup.read_bytes() == original
    assert not list(paths.config.glob(".settings-*.tmp"))


def test_explicit_schema_upgrade_writes_backup(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    paths.config.mkdir(parents=True)
    legacy = b"logging:\n  file_path: data/logs/app.jsonl\n"
    paths.settings.write_bytes(legacy)

    result = upgrade_user_settings(app_paths=paths)

    upgraded = yaml.safe_load(paths.settings.read_text(encoding="utf-8"))
    assert result.changed and result.backup_created
    assert upgraded["schema_version"] == 1
    assert upgraded["logging"]["file_path"] == "app.jsonl"
    assert paths.settings_backup.read_bytes() == legacy


def test_legacy_auto_stt_language_is_migrated_to_chinese_on_explicit_write(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    paths.config.mkdir(parents=True)
    legacy = b"schema_version: 1\nstt:\n  language: auto\n"
    paths.settings.write_bytes(legacy)

    effective = load_settings(app_paths=paths, environ={})
    assert effective.stt.language == "zh"
    assert effective.settings_schema_upgrade_required

    result = upgrade_user_settings(app_paths=paths)

    persisted = yaml.safe_load(paths.settings.read_text(encoding="utf-8"))
    assert result.changed and result.backup_created
    assert persisted["stt"]["language"] == "zh"
