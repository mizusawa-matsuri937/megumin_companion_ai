"""Explicit runtime creation of the private app tree and startup scavenger."""

from __future__ import annotations

from dataclasses import dataclass

from app.paths import AppPaths
from app.temp_assets import TempAssetRegistry, TempScavengeReport
from app.windows_security import DirectorySecurity, directory_security_for_current_platform


@dataclass(frozen=True, slots=True)
class RuntimeStorage:
    temp_registry: TempAssetRegistry
    scavenge_report: TempScavengeReport


def prepare_runtime_storage(
    paths: AppPaths,
    *,
    directory_security: DirectorySecurity | None = None,
    temp_registry: TempAssetRegistry | None = None,
) -> RuntimeStorage:
    """Create runtime-owned directories only after explicit application startup."""

    security = directory_security or directory_security_for_current_platform()
    security.ensure_private_tree(
        paths.root,
        (
            paths.config,
            paths.state,
            paths.secrets,
            paths.logs,
            paths.cache,
            paths.audio_cache,
            paths.temp,
            paths.models,
        ),
    )
    registry = temp_registry or TempAssetRegistry(paths, directory_security=security)
    return RuntimeStorage(
        temp_registry=registry,
        scavenge_report=registry.scavenge(),
    )
