"""DPAPI-backed VTube Studio authentication token storage."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.secret_store import (
    VTS_TOKEN_ID,
    EncryptedSecretFile,
    SecretStoreError,
    SecretStoreErrorCode,
)

_TOKEN_KEYS = {"authentication_token", "plugin_developer", "plugin_name"}
_MAX_LEGACY_TOKEN_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class VTSToken:
    plugin_name: str
    plugin_developer: str
    authentication_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.plugin_name.strip() or not self.plugin_developer.strip():
            raise ValueError("VTS plugin identity 不能为空")
        if not self.authentication_token.strip():
            raise ValueError("VTS authentication token 不能为空")


class TokenStore(Protocol):
    async def load(self) -> VTSToken | None: ...

    async def save(self, token: VTSToken) -> None: ...

    async def delete(self) -> None: ...


class DPAPITokenStore:
    """Persist the complete VTS token record inside one encrypted envelope."""

    def __init__(self, secret_file: EncryptedSecretFile) -> None:
        self._secret_file = secret_file

    async def load(self) -> VTSToken | None:
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> VTSToken | None:
        raw = self._secret_file.read_text()
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != _TOKEN_KEYS:
                raise ValueError("invalid token shape")
            if not all(isinstance(value, str) for value in payload.values()):
                raise ValueError("invalid token value")
            return VTSToken(
                plugin_name=payload["plugin_name"],
                plugin_developer=payload["plugin_developer"],
                authentication_token=payload["authentication_token"],
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SecretStoreError(SecretStoreErrorCode.corrupt, VTS_TOKEN_ID) from exc

    async def save(self, token: VTSToken) -> None:
        await asyncio.to_thread(self._save_sync, token)

    def _save_sync(self, token: VTSToken) -> None:
        self._secret_file.write_text(
            json.dumps(
                {
                    "authentication_token": token.authentication_token,
                    "plugin_developer": token.plugin_developer,
                    "plugin_name": token.plugin_name,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    async def delete(self) -> None:
        await asyncio.to_thread(self._secret_file.revoke)


def read_legacy_plaintext_token(path: Path) -> VTSToken:
    """Read one explicitly selected legacy token without scanning any directory."""

    try:
        if path.stat().st_size > _MAX_LEGACY_TOKEN_BYTES:
            raise ValueError("legacy token too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != _TOKEN_KEYS:
            raise ValueError("invalid legacy token shape")
        if not all(isinstance(value, str) for value in payload.values()):
            raise ValueError("invalid legacy token value")
        return VTSToken(
            plugin_name=payload["plugin_name"],
            plugin_developer=payload["plugin_developer"],
            authentication_token=payload["authentication_token"],
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SecretStoreError(SecretStoreErrorCode.corrupt, VTS_TOKEN_ID) from exc
