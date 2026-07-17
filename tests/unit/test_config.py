"""Configuration loading and missing-secret behavior."""

from importlib import resources
from pathlib import Path

import pytest
import yaml
from app.config import ConfigurationError, load_settings, write_user_settings
from app.paths import AppPaths


def local_paths(tmp_path: Path) -> AppPaths:
    return AppPaths.from_local_app_data(tmp_path / "Local AppData")


def write_config(path: Path, *, provider: str = "none", extra: str = "") -> None:
    path.write_text(
        f"""
app:
  log_level: INFO
server:
  port: 8765
logging:
  console_enabled: false
  file_enabled: false
llm:
  provider: {provider}
  api_key_env: TEST_LLM_KEY
{extra}
""".strip(),
        encoding="utf-8",
    )


def test_loads_yaml_and_environment_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)

    settings = load_settings(
        config_path,
        environ={
            "MEGUMIN_SERVER_PORT": "9010",
            "MEGUMIN_LOG_LEVEL": "DEBUG",
            "TEST_LLM_KEY": "secret-not-for-serialization",
        },
        app_paths=local_paths(tmp_path),
    )

    assert settings.server.port == 9010
    assert settings.app.log_level == "DEBUG"
    assert "secret-not-for-serialization" not in settings.model_dump_json()


def test_dotenv_is_loaded_but_process_environment_wins(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    write_config(config_path)
    env_path.write_text("MEGUMIN_SERVER_PORT=9000\nTEST_LLM_KEY=file-secret\n", encoding="utf-8")

    settings = load_settings(
        config_path,
        env_path,
        environ={"MEGUMIN_SERVER_PORT": "9001", "TEST_LLM_KEY": "process-secret"},
        app_paths=local_paths(tmp_path),
    )

    assert settings.server.port == 9001
    assert settings.require_secret("TEST_LLM_KEY").get_secret_value() == "process-secret"


def test_dotenv_is_never_discovered_implicitly(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    (tmp_path / ".env").write_text("MEGUMIN_SERVER_PORT=9999\n", encoding="utf-8")

    settings = load_settings(config_path, environ={}, app_paths=local_paths(tmp_path))

    assert settings.server.port == 8765


def test_missing_config_has_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="开发配置 missing.yaml不存在") as error:
        load_settings(tmp_path / "missing.yaml", environ={}, app_paths=local_paths(tmp_path))
    assert str(tmp_path) not in str(error.value)


def test_enabled_provider_secret_check_is_deferred_without_disk_access(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, provider="deepseek")
    paths = local_paths(tmp_path)

    settings = load_settings(config_path, environ={}, app_paths=paths)

    assert not paths.root.exists()
    with pytest.raises(ConfigurationError, match="TEST_LLM_KEY") as error:
        settings.require_llm_api_key()
    assert "--env-file" in str(error.value)
    assert "DPAPI" in str(error.value)


def test_placeholder_is_not_accepted_as_a_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, provider="deepseek")

    settings = load_settings(
        config_path,
        environ={"TEST_LLM_KEY": "replace_me"},
        app_paths=local_paths(tmp_path),
    )
    with pytest.raises(ConfigurationError, match="缺少必需的密钥"):
        settings.require_llm_api_key()


def test_unknown_config_key_is_reported(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, extra="unexpected_section: true")

    with pytest.raises(ConfigurationError, match="unexpected_section"):
        load_settings(config_path, environ={}, app_paths=local_paths(tmp_path))


def test_stt_environment_overrides_are_typed_and_do_not_enable_by_default(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    paths = local_paths(tmp_path)
    defaults = load_settings(config_path, environ={}, app_paths=paths)
    assert not defaults.stt.enabled

    settings = load_settings(
        config_path,
        environ={
            "MEGUMIN_STT_ENABLED": "true",
            "MEGUMIN_STT_EXECUTABLE": "local/whisper-cli",
            "MEGUMIN_STT_MODEL_PATH": "local/model.bin",
        },
        app_paths=paths,
    )
    assert settings.stt.enabled
    assert settings.stt.executable == Path("local/whisper-cli")
    assert settings.stt.model_path == Path("local/model.bin")


def test_packaged_defaults_load_from_arbitrary_unicode_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "任意 目录"
    outside.mkdir()
    monkeypatch.chdir(outside)

    paths = local_paths(tmp_path)
    settings = load_settings(environ={}, app_paths=paths)

    assert settings.config_source == "内置默认配置"
    assert settings.paths == paths
    assert settings.llm.provider == "mock"
    assert settings.pipeline.playback_mode == "silent"
    assert settings.database_path() == paths.state / "companion.sqlite3"
    assert list(outside.iterdir()) == []
    assert not paths.root.exists()


def test_explicit_config_does_not_change_managed_runtime_paths(tmp_path: Path) -> None:
    config_dir = tmp_path / "配置 空格"
    config_dir.mkdir()
    config_path = config_dir / "custom.yaml"
    write_config(config_path)

    paths = local_paths(tmp_path)
    settings = load_settings(config_path, environ={}, app_paths=paths)

    assert settings.config_source == "内置默认配置 + 开发配置 custom.yaml"
    assert settings.paths == paths
    assert settings.log_file_path() == paths.logs / "app.jsonl"


def test_layer_order_is_defaults_user_cli_then_environment(tmp_path: Path) -> None:
    paths = local_paths(tmp_path)
    write_user_settings({"schema_version": 1, "server": {"port": 8800}}, app_paths=paths)
    explicit = tmp_path / "dev.yaml"
    explicit.write_text("schema_version: 1\nserver:\n  port: 8900\n", encoding="utf-8")

    settings = load_settings(
        explicit,
        environ={"MEGUMIN_SERVER_PORT": "9000"},
        app_paths=paths,
    )

    assert settings.server.port == 9000
    assert settings.config_source == ("内置默认配置 + 用户设置 + 开发配置 dev.yaml + 开发环境覆盖")


def test_legacy_user_settings_upgrade_only_in_memory(tmp_path: Path) -> None:
    paths = local_paths(tmp_path)
    paths.config.mkdir(parents=True)
    legacy = "logging:\n  file_path: data/logs/app.jsonl\n"
    paths.settings.write_text(legacy, encoding="utf-8")

    settings = load_settings(environ={}, app_paths=paths)

    assert settings.settings_schema_upgrade_required
    assert settings.log_file_path() == paths.logs / "app.jsonl"
    assert paths.settings.read_text(encoding="utf-8") == legacy


def test_future_user_settings_schema_is_rejected_without_path_leak(tmp_path: Path) -> None:
    paths = local_paths(tmp_path)
    paths.config.mkdir(parents=True)
    paths.settings.write_text("schema_version: 999\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="较新的设置 schema_version=999") as error:
        load_settings(environ={}, app_paths=paths)

    assert str(tmp_path) not in str(error.value)


def test_production_user_settings_reject_development_overrides(tmp_path: Path) -> None:
    paths = local_paths(tmp_path)
    write_user_settings(
        {"schema_version": 1, "app": {"environment": "prod"}},
        app_paths=paths,
    )

    with pytest.raises(ConfigurationError, match="生产设置禁止"):
        load_settings(
            environ={"MEGUMIN_SERVER_PORT": "9000"},
            app_paths=paths,
        )


def test_repository_config_matches_packaged_default() -> None:
    packaged = (
        resources.files("app.resources").joinpath("default_config.yaml").read_text(encoding="utf-8")
    )
    repository = (Path(__file__).parents[2] / "config.yaml").read_text(encoding="utf-8")

    assert yaml.safe_load(packaged) == yaml.safe_load(repository)
