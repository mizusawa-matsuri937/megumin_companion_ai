"""DPAPI envelope and explicit legacy-import tests for VTS tokens."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from app.clients.vts.token_store import (
    DPAPITokenStore,
    VTSToken,
    read_legacy_plaintext_token,
)
from app.paths import AppPaths
from app.secret_store import SecretStoreError, vts_token_file
from app.windows_security import PortableDirectorySecurity


class _ReversingProtector:
    @property
    def algorithm(self) -> str:
        return "test-reverse"

    @property
    def scope(self) -> str:
        return "current_user"

    def protect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        del purpose, key_id
        return value[::-1]

    def unprotect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        del purpose, key_id
        return value[::-1]


def _store(tmp_path: Path) -> tuple[DPAPITokenStore, Path]:
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    path = paths.secrets / "vts-token.json"
    encrypted = vts_token_file(
        paths,
        path,
        protector=_ReversingProtector(),
        directory_security=PortableDirectorySecurity(),
    )
    return DPAPITokenStore(encrypted), path


def test_token_store_round_trip_is_atomic_and_token_repr_is_redacted(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, path = _store(tmp_path)
        token = VTSToken("Companion", "Local User", "never-log-this-token")

        await store.save(token)

        assert await store.load() == token
        assert "never-log-this-token" not in repr(token)
        assert "never-log-this-token" not in path.read_text(encoding="ascii")
        assert list(path.parent.glob("*.part")) == []

        await store.delete()
        await store.delete()
        assert not path.exists()

    asyncio.run(scenario())


def test_corrupt_token_payload_is_preserved_until_explicit_reset(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, path = _store(tmp_path)
        secret_file = store._secret_file
        secret_file.write_text(
            json.dumps(
                {
                    "plugin_name": "Companion",
                    "plugin_developer": "Local User",
                    "authentication_token": "secret",
                    "unexpected": "field",
                }
            )
        )

        with pytest.raises(SecretStoreError):
            await store.load()
        assert path.exists()

        await store.delete()
        assert not path.exists()

    asyncio.run(scenario())


def test_legacy_plaintext_token_is_read_only_from_explicit_file(tmp_path: Path) -> None:
    path = tmp_path / "explicit-old-token.json"
    path.write_text(
        json.dumps(
            {
                "plugin_name": "Companion",
                "plugin_developer": "Local User",
                "authentication_token": "legacy-secret",
            }
        ),
        encoding="utf-8",
    )

    token = read_legacy_plaintext_token(path)

    assert token.authentication_token == "legacy-secret"
    assert path.exists(), "读取预检不能静默删除用户显式选择的源文件"
    path.write_text('{"authentication_token":', encoding="utf-8")
    with pytest.raises(SecretStoreError) as captured:
        read_legacy_plaintext_token(path)
    assert "legacy-secret" not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)
