"""Explicit secret-import, replacement, revoke, and reset CLI contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import cli
from app.config import Settings
from app.config.settings import LoggingConfig
from app.paths import AppPaths
from app.secret_store import LLM_API_KEY_ID, VTS_TOKEN_ID


class _MemorySecretFile:
    def __init__(self, key_id: str, *, initial: str | None = None) -> None:
        self.value = initial
        self.metadata = SimpleNamespace(key_id=key_id, scope="current_user")

    @property
    def exists(self) -> bool:
        return self.value is not None

    def write_text(self, value: str) -> object:
        self.value = value
        return self.metadata

    def read_text(self) -> str | None:
        return self.value

    def revoke(self) -> bool:
        changed = self.value is not None
        self.value = None
        return changed

    def reset(self) -> bool:
        return self.revoke()


def _secret_settings(tmp_path: Path) -> Settings:
    settings = Settings(logging=LoggingConfig(console_enabled=False, file_enabled=False))
    settings._paths = AppPaths(root=tmp_path / "LocalAppData" / "MeguminCompanion")
    return settings


def test_secret_action_load_discovers_path_but_ignores_process_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths(root=tmp_path / "private")
    settings = _secret_settings(tmp_path)
    calls: list[tuple[dict[str, str], AppPaths]] = []
    monkeypatch.setattr(AppPaths, "discover", lambda: paths)

    def fake_load_settings(*, environ: dict[str, str], app_paths: AppPaths) -> Settings:
        calls.append((environ, app_paths))
        return settings

    monkeypatch.setattr(cli, "load_settings", fake_load_settings)

    assert cli._load_production_settings_for_secret_action() is settings
    assert calls == [({}, paths)]


def test_explicit_llm_environment_import_replaces_without_echoing_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _secret_settings(tmp_path)
    secret = "llm-key-must-never-appear"
    store = _MemorySecretFile(LLM_API_KEY_ID, initial="old-value")
    monkeypatch.setattr(cli, "_load_production_settings_for_secret_action", lambda: settings)
    monkeypatch.setattr(cli, "llm_api_key_file", lambda _paths: store)
    monkeypatch.setenv("W03_LLM_KEY", secret)

    assert cli.main(["--import-llm-key-env", "W03_LLM_KEY"]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert store.value == secret
    assert payload["secret_id"] == LLM_API_KEY_ID
    assert payload["scope"] == "current_user"
    assert payload["replaced"] is True
    assert secret not in output
    assert str(tmp_path) not in output
    assert "W03_LLM_KEY" not in os.environ


def test_explicit_vts_plaintext_import_verifies_and_optionally_deletes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _secret_settings(tmp_path)
    token = "vts-token-must-never-appear"
    source = tmp_path / "legacy private token.json"
    source.write_text(
        json.dumps(
            {
                "authentication_token": token,
                "plugin_developer": "Local Developer",
                "plugin_name": "Megumin Companion",
            }
        ),
        encoding="utf-8",
    )
    store = _MemorySecretFile(VTS_TOKEN_ID)
    monkeypatch.setattr(cli, "_load_production_settings_for_secret_action", lambda: settings)
    monkeypatch.setattr(cli, "vts_token_file", lambda _paths, _path: store)

    assert cli.main(["--import-vts-token", str(source), "--delete-import-source"]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["secret_id"] == VTS_TOKEN_ID
    assert payload["plaintext_source_removed"] is True
    assert payload["physical_erasure_guaranteed"] is False
    assert not source.exists()
    assert store.value is not None and token in store.value
    assert token not in output
    assert str(tmp_path) not in output


def test_in_place_vts_import_requires_explicit_plaintext_removal_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _secret_settings(tmp_path)
    token = "retained-until-explicit-confirmation"
    source = settings.vts_token_path()
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "authentication_token": token,
                "plugin_developer": "Local Developer",
                "plugin_name": "Megumin Companion",
            }
        ),
        encoding="utf-8",
    )
    store = _MemorySecretFile(VTS_TOKEN_ID)
    monkeypatch.setattr(cli, "_load_production_settings_for_secret_action", lambda: settings)
    monkeypatch.setattr(cli, "vts_token_file", lambda _paths, _path: store)

    assert cli.main(["--import-vts-token", str(source)]) == 2

    output = capsys.readouterr().err
    assert source.exists()
    assert store.value is None
    assert token not in output
    assert str(tmp_path) not in output
    assert "--delete-import-source" in output


@pytest.mark.parametrize("action", ["--revoke-secret", "--reset-secret"])
def test_secret_delete_actions_report_only_stable_id(
    action: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _secret_settings(tmp_path)
    store = _MemorySecretFile(LLM_API_KEY_ID, initial="delete-me-without-echo")
    monkeypatch.setattr(cli, "_load_production_settings_for_secret_action", lambda: settings)
    monkeypatch.setattr(cli, "llm_api_key_file", lambda _paths: store)

    assert cli.main([action, LLM_API_KEY_ID]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["secret_id"] == LLM_API_KEY_ID
    assert payload["changed"] is True
    assert payload["physical_erasure_guaranteed"] is False
    assert "delete-me-without-echo" not in output
