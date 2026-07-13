"""Security and corruption tests for the local VTS token store."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from app.clients.vts.token_store import FileTokenStore, VTSToken


def test_token_store_round_trip_is_atomic_and_token_repr_is_redacted(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "nested" / "token.json"
        store = FileTokenStore(path)
        token = VTSToken("Companion", "Local User", "never-log-this-token")

        await store.save(token)

        assert await store.load() == token
        assert "never-log-this-token" not in repr(token)
        assert list(path.parent.glob("*.tmp")) == []
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["authentication_token"] == "never-log-this-token"

        await store.delete()
        await store.delete()
        assert not path.exists()

    asyncio.run(scenario())


def test_corrupt_or_unexpected_token_file_is_deleted(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "token.json"
        store = FileTokenStore(path)

        path.write_text('{"authentication_token":', encoding="utf-8")
        assert await store.load() is None
        assert not path.exists()

        path.write_text(
            json.dumps(
                {
                    "plugin_name": "Companion",
                    "plugin_developer": "Local User",
                    "authentication_token": "secret",
                    "unexpected": "field",
                }
            ),
            encoding="utf-8",
        )
        assert await store.load() is None
        assert not path.exists()

    asyncio.run(scenario())
