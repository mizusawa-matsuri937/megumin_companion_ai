"""Configuration loading and missing-secret behavior."""

from importlib import resources
from pathlib import Path

import pytest
import yaml
from app.config import ConfigurationError, load_settings


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
        tmp_path / ".env",
        environ={
            "MEGUMIN_SERVER_PORT": "9010",
            "MEGUMIN_LOG_LEVEL": "DEBUG",
            "TEST_LLM_KEY": "secret-not-for-serialization",
        },
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
    )

    assert settings.server.port == 9001
    assert settings.require_secret("TEST_LLM_KEY").get_secret_value() == "process-secret"


def test_missing_config_has_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="配置文件 missing.yaml不存在") as error:
        load_settings(tmp_path / "missing.yaml", tmp_path / ".env", environ={})
    assert str(tmp_path) not in str(error.value)


def test_enabled_provider_requires_named_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, provider="deepseek")

    with pytest.raises(ConfigurationError, match="TEST_LLM_KEY") as error:
        load_settings(config_path, tmp_path / ".env", environ={})

    assert ".env.example" in str(error.value)
    assert "不要把 .env 提交" in str(error.value)


def test_placeholder_is_not_accepted_as_a_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, provider="deepseek")

    with pytest.raises(ConfigurationError, match="缺少必需的密钥环境变量"):
        load_settings(
            config_path,
            tmp_path / ".env",
            environ={"TEST_LLM_KEY": "replace_me"},
        )


def test_unknown_config_key_is_reported(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, extra="unexpected_section: true")

    with pytest.raises(ConfigurationError, match="unexpected_section"):
        load_settings(config_path, tmp_path / ".env", environ={})


def test_stt_environment_overrides_are_typed_and_do_not_enable_by_default(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    defaults = load_settings(config_path, tmp_path / ".env", environ={})
    assert not defaults.stt.enabled

    settings = load_settings(
        config_path,
        tmp_path / ".env",
        environ={
            "MEGUMIN_STT_ENABLED": "true",
            "MEGUMIN_STT_EXECUTABLE": "local/whisper-cli",
            "MEGUMIN_STT_MODEL_PATH": "local/model.bin",
        },
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

    settings = load_settings(env_path=outside / "missing.env", environ={})

    assert settings.config_source == "内置默认配置"
    assert settings.runtime_base == outside
    assert settings.llm.provider == "mock"
    assert settings.pipeline.playback_mode == "silent"
    assert settings.resolve_runtime_path(Path("data/test.bin")) == outside / "data/test.bin"
    assert list(outside.iterdir()) == []


def test_explicit_config_owns_relative_runtime_base(tmp_path: Path) -> None:
    config_dir = tmp_path / "配置 空格"
    config_dir.mkdir()
    config_path = config_dir / "custom.yaml"
    write_config(config_path)

    settings = load_settings(config_path, config_dir / "missing.env", environ={})

    assert settings.config_source == "配置文件 custom.yaml"
    assert settings.runtime_base == config_dir
    assert settings.resolve_runtime_path(Path("relative/item.bin")) == (
        config_dir / "relative/item.bin"
    )


def test_repository_config_matches_packaged_default() -> None:
    packaged = (
        resources.files("app.resources").joinpath("default_config.yaml").read_text(encoding="utf-8")
    )
    repository = (Path(__file__).parents[2] / "config.yaml").read_text(encoding="utf-8")

    assert yaml.safe_load(packaged) == yaml.safe_load(repository)
