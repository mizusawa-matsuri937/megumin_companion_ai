"""Safety guard for the human redaction-check command."""

from pathlib import Path

import pytest
from app.config import ConfigurationError, Settings
from app.config.redaction_check import run


def test_manual_check_refuses_non_fake_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANION_LLM_API_KEY", "looks-like-a-real-secret")
    monkeypatch.setattr("app.config.redaction_check.load_settings", _test_settings)

    with pytest.raises(ConfigurationError, match="请勿用真实"):
        run()


def _test_settings() -> Settings:
    settings = Settings()
    settings._environment = {"COMPANION_LLM_API_KEY": "looks-like-a-real-secret"}
    return settings
