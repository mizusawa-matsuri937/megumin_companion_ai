"""Local, atomic storage for VTube Studio authentication tokens."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4


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


class FileTokenStore:
    """Persist one token with restrictive permissions and atomic replacement."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def load(self) -> VTSToken | None:
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> VTSToken | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {
                "plugin_name",
                "plugin_developer",
                "authentication_token",
            }:
                raise ValueError("invalid token shape")
            if not all(isinstance(value, str) for value in payload.values()):
                raise ValueError("invalid token value")
            return VTSToken(
                plugin_name=payload["plugin_name"],
                plugin_developer=payload["plugin_developer"],
                authentication_token=payload["authentication_token"],
            )
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            # A corrupt token cannot be authenticated and must not remain as a
            # repeatedly parsed secret-bearing artifact.
            with suppress(OSError):
                self._path.unlink(missing_ok=True)
            return None

    async def save(self, token: VTSToken) -> None:
        await asyncio.to_thread(self._save_sync, token)

    def _save_sync(self, token: VTSToken) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            os.chmod(self._path.parent, 0o700)
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        payload = {
            "plugin_name": token.plugin_name,
            "plugin_developer": token.plugin_developer,
            "authentication_token": token.authentication_token,
        }
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            with suppress(OSError):
                os.chmod(self._path, 0o600)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    async def delete(self) -> None:
        await asyncio.to_thread(self._path.unlink, missing_ok=True)
