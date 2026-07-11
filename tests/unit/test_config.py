"""Configuration loading and missing-secret behavior."""

from pathlib import Path

import pytest
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
    with pytest.raises(ConfigurationError, match="配置文件不存在"):
        load_settings(tmp_path / "missing.yaml", tmp_path / ".env", environ={})


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
