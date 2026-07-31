from __future__ import annotations

import os

import pytest
from app.tts_gateway import entrypoint


def test_gateway_entrypoint_forces_offline_dependency_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (*entrypoint._OFFLINE_ENVIRONMENT, "PYTHONDONTWRITEBYTECODE"):
        monkeypatch.setenv(name, "0")

    entrypoint._enforce_offline_environment()

    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert all(os.environ[name] == value for name, value in entrypoint._OFFLINE_ENVIRONMENT.items())
