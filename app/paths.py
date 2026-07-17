"""Central, side-effect-free application path policy.

The production application owns one per-user LocalAppData tree.  Merely
constructing :class:`AppPaths` never creates that tree, which keeps imports and
configuration preflight free of filesystem side effects.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePath

APP_DIRECTORY_NAME = "MeguminCompanion"
RESOURCE_PACKAGE = "app.resources"


class AppPathError(ValueError):
    """Raised when a managed application path would escape its category root."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    """All package and per-user path categories used by the application."""

    root: Path

    @classmethod
    def from_local_app_data(cls, local_app_data: Path | str) -> AppPaths:
        """Build paths below an explicit LocalAppData base without touching disk."""

        base = Path(local_app_data).expanduser()
        if not base.is_absolute():
            raise AppPathError("LocalAppData 必须是绝对路径。")
        return cls(root=base / APP_DIRECTORY_NAME)

    @classmethod
    def discover(cls, environ: Mapping[str, str] | None = None) -> AppPaths:
        """Discover the current user's data base without consulting the CWD.

        ``LOCALAPPDATA`` is the supported Windows contract and also makes
        redirected-profile behavior deterministic in tests.  Non-Windows
        fallbacks exist only so source and wheel quality gates remain portable.
        """

        environment = os.environ if environ is None else environ
        local_app_data = environment.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return cls.from_local_app_data(local_app_data)
        if os.name == "nt":
            raise AppPathError("无法定位当前用户的 LocalAppData。")

        xdg_data_home = environment.get("XDG_DATA_HOME", "").strip()
        if xdg_data_home:
            return cls.from_local_app_data(xdg_data_home)
        home = Path.home()
        if sys.platform == "darwin":
            return cls.from_local_app_data(home / "Library" / "Application Support")
        return cls.from_local_app_data(home / ".local" / "share")

    @property
    def resource_root(self) -> Traversable:
        return resources.files(RESOURCE_PACKAGE)

    def resource(self, relative_name: str) -> Traversable:
        """Return one packaged read-only resource after lexical validation."""

        candidate = PurePath(relative_name)
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise AppPathError("package resource 名称必须是包内相对路径。")
        return self.resource_root.joinpath(*candidate.parts)

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def settings(self) -> Path:
        return self.config / "settings.yaml"

    @property
    def settings_backup(self) -> Path:
        return self.config / "settings.yaml.bak"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def audio_cache(self) -> Path:
        return self.cache / "audio"

    @property
    def temp(self) -> Path:
        return self.root / "temp"

    @property
    def models(self) -> Path:
        return self.root / "models"

    def managed(self, category_root: Path, value: Path | str, *, category: str) -> Path:
        """Resolve a relative managed path and reject absolute/parent traversal."""

        candidate = Path(value)
        if candidate.is_absolute() or candidate.anchor:
            raise AppPathError(f"{category} 路径必须位于应用的受管目录内。")
        if not candidate.parts or ".." in candidate.parts:
            raise AppPathError(f"{category} 路径不能离开应用的受管目录。")
        return category_root.joinpath(*candidate.parts)

    def external_or_model(self, value: Path | str, *, category: str) -> Path:
        """Preserve an explicit external path or resolve a relative model asset."""

        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
        return self.managed(self.models, candidate, category=category)

    def migration_staging_root(self, migration_id: str) -> Path:
        candidate = PurePath(migration_id)
        if len(candidate.parts) != 1 or candidate.name != migration_id or not migration_id:
            raise AppPathError("migration id 无效。")
        return self.root.parent / f"{self.root.name}.migration-{migration_id}"
