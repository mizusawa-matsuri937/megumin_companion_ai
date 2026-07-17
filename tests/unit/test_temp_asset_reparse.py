"""Configured STT-root discovery and reparse-point escape defenses."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from app.paths import AppPaths
from app.temp_assets import (
    TempAssetKind,
    TempAssetRegistry,
    TempDeleteStatus,
)
from app.windows_security import PortableDirectorySecurity


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")


def _registry(paths: AppPaths, *, minimum_age: float = 0.0) -> TempAssetRegistry:
    return TempAssetRegistry(
        paths,
        clock=lambda: 1_000.0,
        minimum_scavenge_age_seconds=minimum_age,
        directory_security=PortableDirectorySecurity(),
    )


def _create_directory_reparse(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlink unavailable: {symlink_error}")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip("directory junction unavailable")


def test_scavenger_finds_strict_stt_directory_under_configured_nested_root(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    registry = _registry(paths, minimum_age=100.0)
    directory = paths.temp / "custom-stt" / "nested" / f"companion-stt-{'f' * 32}"
    directory.mkdir(parents=True)
    (directory / "transcript.json").write_text('{"private":"temporary"}', encoding="utf-8")
    os.utime(directory, (100.0, 100.0))

    report = registry.scavenge()

    assert report.discovered == 1
    assert report.deleted == 1
    assert not directory.exists()


def test_registered_path_replaced_by_reparse_point_never_deletes_outside(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    registry = _registry(paths)
    path = paths.temp / "audio" / "mock" / ("a" * 16) / f"0000-{'b' * 20}.wav"
    entry = registry.register(path, TempAssetKind.mock_wav)
    path.parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / path.name
    outside_file.write_bytes(b"must remain outside")
    _create_directory_reparse(path.parent, outside)

    result = registry.delete(entry.asset_id, ignore_retry_deadline=True)

    assert result.status is TempDeleteStatus.rejected
    assert outside_file.read_bytes() == b"must remain outside"
    assert [item.asset_id for item in registry.entries()] == [entry.asset_id]
    path.parent.rmdir()


def test_reparse_child_rejects_whole_directory_before_partial_deletion(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    registry = _registry(paths)
    directory = paths.temp / "custom" / f"companion-stt-{'c' * 32}"
    entry = registry.register(directory, TempAssetKind.stt_directory)
    directory.mkdir()
    local_file = directory / "transcript.json"
    local_file.write_text("private transcript", encoding="utf-8")
    outside = tmp_path / "outside-child"
    outside.mkdir()
    outside_file = outside / "keep.txt"
    outside_file.write_text("keep", encoding="utf-8")
    alias = directory / "alias"
    _create_directory_reparse(alias, outside)

    result = registry.delete(entry.asset_id, ignore_retry_deadline=True)

    assert result.status is TempDeleteStatus.rejected
    assert local_file.read_text(encoding="utf-8") == "private transcript"
    assert outside_file.read_text(encoding="utf-8") == "keep"
    alias.rmdir()
    assert (
        registry.delete(entry.asset_id, ignore_retry_deadline=True).status
        is TempDeleteStatus.deleted
    )
