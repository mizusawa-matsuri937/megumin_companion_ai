"""Runtime-only private directory creation and startup scavenger wiring."""

from __future__ import annotations

import os
from pathlib import Path

from app.paths import AppPaths
from app.runtime_storage import prepare_runtime_storage
from app.temp_assets import TempAssetKind, TempAssetRegistry
from app.windows_security import PortableDirectorySecurity


def test_prepare_runtime_storage_creates_categories_and_scavenges_crash_residue(
    tmp_path: Path,
) -> None:
    paths = AppPaths(root=tmp_path / "LocalAppData" / "MeguminCompanion")
    security = PortableDirectorySecurity()
    registry = TempAssetRegistry(
        paths,
        clock=lambda: 1_000.0,
        minimum_scavenge_age_seconds=0.0,
        directory_security=security,
    )
    residue = paths.temp / "audio" / "mock" / ("a" * 16) / f"0000-{'b' * 20}.wav"
    entry = registry.register(residue, TempAssetKind.mock_wav)
    residue.write_bytes(b"crash residue")
    os.utime(residue, (100.0, 100.0))

    runtime = prepare_runtime_storage(
        paths,
        directory_security=security,
        temp_registry=registry,
    )

    expected = (
        paths.config,
        paths.state,
        paths.secrets,
        paths.logs,
        paths.cache,
        paths.audio_cache,
        paths.temp,
        paths.models,
    )
    assert all(path.is_dir() for path in expected)
    assert runtime.temp_registry is registry
    assert runtime.scavenge_report.deleted == 1
    assert not residue.exists()
    assert all(item.asset_id != entry.asset_id for item in registry.entries())
