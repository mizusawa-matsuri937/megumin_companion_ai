"""W01 side-effect-free CLI, ASGI factory, and desktop preflight contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from app import __version__, cli
from app.config import Settings
from app.config.settings import LoggingConfig, StorageConfig
from app.config.user_settings import UserSettingsWriteResult
from app.legacy_migration import LegacyMigrationError, LegacyMigrationResult
from desktop_client import entrypoint as desktop_entrypoint


def _must_not_run(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("runtime construction must not run for this command")


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_help_and_version_do_not_load_configuration(
    argument: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_load_settings", _must_not_run)

    with pytest.raises(SystemExit) as error:
        cli.main([argument])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "megumin-companion-api" in output
    if argument == "--version":
        assert __version__ in output


def test_no_action_prints_help_without_loading_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_load_settings", _must_not_run)

    assert cli.main([]) == 0

    assert "--check-config" in capsys.readouterr().out


def test_check_config_does_not_construct_runtime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=False),
    )
    monkeypatch.setattr(cli, "_load_settings", lambda *_args: settings)
    monkeypatch.setattr(cli, "create_app", _must_not_run)
    monkeypatch.setattr(cli, "_run_server", _must_not_run)

    assert cli.main(["--check-config"]) == 0

    output = capsys.readouterr().out
    assert '"status": "ok"' in output
    assert '"schema_version": 1' in output
    assert '"schema_upgrade_required": false' in output
    assert '"storage_enabled": false' in output


def test_check_config_reports_safe_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "private-user-name" / "missing.yaml"

    assert cli.main(["--config", str(missing), "--check-config"]) == 2

    output = capsys.readouterr().err
    assert "configuration_error" in output
    assert "missing.yaml" in output
    assert "private-user-name" not in output


def test_explicit_serve_constructs_one_app_and_passes_network_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=False),
    )
    application = object()
    calls: list[tuple[object, str, int, object]] = []
    monkeypatch.setattr(cli, "_load_settings", lambda *_args: settings)
    monkeypatch.setattr(
        cli, "create_app", lambda received: application if received is settings else None
    )

    def record_run(app: object, *, host: str, port: int) -> None:
        calls.append((app, host, port, None))

    monkeypatch.setattr(cli, "_run_server", record_run)

    assert cli.main(["--serve", "--host", "127.0.0.2", "--port", "9011"]) == 0

    assert calls == [(application, "127.0.0.2", 9011, None)]


@pytest.mark.parametrize("port", [0, 65_536])
def test_serve_rejects_invalid_port_before_runtime_construction(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=False),
    )
    monkeypatch.setattr(cli, "_load_settings", lambda *_args: settings)
    monkeypatch.setattr(cli, "create_app", _must_not_run)

    with pytest.raises(SystemExit) as error:
        cli.main(["--serve", "--port", str(port)])

    assert error.value.code == 2


def test_app_main_exports_factory_without_global_application() -> None:
    import app.main as main_module

    assert callable(main_module.create_app)
    assert "app" not in vars(main_module)


def test_desktop_entrypoint_is_honest_preflight_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(desktop_entrypoint, "check_configuration", _must_not_run)

    assert desktop_entrypoint.main([]) == 3

    output = capsys.readouterr().err
    assert "desktop_unavailable" in output
    assert "W13" in output


def test_desktop_check_config_delegates_without_starting_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path | None, Path | None]] = []

    def record_check(config: Path | None, env_file: Path | None) -> int:
        calls.append((config, env_file))
        return 0

    monkeypatch.setattr(desktop_entrypoint, "check_configuration", record_check)

    assert desktop_entrypoint.main(["--check-config"]) == 0
    assert calls == [(None, None)]


def test_migration_action_is_explicit_and_does_not_load_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "old data"
    calls: list[Path] = []

    def record_migration(path: Path) -> int:
        calls.append(path)
        return 0

    monkeypatch.setattr(cli, "_load_settings", _must_not_run)
    monkeypatch.setattr(cli, "_migrate_old_data", record_migration)

    assert cli.main(["--migrate-from", str(source)]) == 0

    assert calls == [source]


def test_settings_upgrade_rejects_development_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_upgrade_local_settings", _must_not_run)

    with pytest.raises(SystemExit) as error:
        cli.main(["--upgrade-settings", "--config", "dev.yaml"])

    assert error.value.code == 2


def test_settings_upgrade_reports_backup_without_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "upgrade_user_settings",
        lambda: UserSettingsWriteResult(changed=True, backup_created=True),
    )

    assert cli.main(["--upgrade-settings"]) == 0

    output = capsys.readouterr().out
    assert '"backup_created": true' in output
    assert "settings.yaml" not in output


def test_migration_wrapper_reports_safe_result_and_manual_next_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = object()
    monkeypatch.setattr("app.cli.AppPaths.discover", lambda: paths)
    monkeypatch.setattr(
        cli,
        "migrate_legacy_data",
        lambda source, *, app_paths: LegacyMigrationResult(
            database_migrated=source.name == "data" and app_paths is paths,
            model_file_count=1,
            backup_created=True,
            source_preserved=True,
            target_activated=True,
            secrets_require_reentry=True,
        ),
    )

    assert cli.main(["--migrate-from", str(tmp_path / "data")]) == 0

    output = capsys.readouterr().out
    assert '"source_preserved": true' in output
    assert "再手动删除旧 data" in output
    assert str(tmp_path) not in output


def test_migration_wrapper_returns_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("app.cli.AppPaths.discover", lambda: object())

    def fail_migration(*_args: object, **_kwargs: object) -> None:
        raise LegacyMigrationError("目标已存在；不会覆盖")

    monkeypatch.setattr(cli, "migrate_legacy_data", fail_migration)

    assert cli.main(["--migrate-from", str(tmp_path / "private" / "data")]) == 2

    output = capsys.readouterr().err
    assert "不会覆盖" in output
    assert str(tmp_path) not in output
