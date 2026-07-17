"""Registry, retry, crash recovery, and path-boundary tests for temp assets."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.paths import AppPaths
from app.temp_assets import (
    TempAssetKind,
    TempAssetRegistry,
    TempDeleteStatus,
    TempRegistryError,
)
from app.windows_security import PortableDirectorySecurity

_TURN = "a" * 16
_JOB = "b" * 20


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")


def _registry(
    paths: AppPaths,
    *,
    now: list[float] | None = None,
    minimum_age: float = 0.0,
) -> TempAssetRegistry:
    clock = (lambda: now[0]) if now is not None else (lambda: 1_000.0)
    return TempAssetRegistry(
        paths,
        clock=clock,
        minimum_scavenge_age_seconds=minimum_age,
        retry_base_seconds=0.5,
        retry_max_seconds=4.0,
        directory_security=PortableDirectorySecurity(),
    )


def _mock_wav(paths: AppPaths) -> Path:
    return paths.temp / "audio" / "mock" / _TURN / f"0000-{_JOB}.wav"


def test_registry_uses_relative_metadata_and_unregisters_only_after_delete(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    registry = _registry(paths)
    path = _mock_wav(paths)

    entry = registry.register(path, TempAssetKind.mock_wav)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"temporary wav")

    payload = paths.state.joinpath("temp-assets.json").read_text(encoding="ascii")
    assert str(tmp_path) not in payload
    assert entry.relative_path == f"audio/mock/{_TURN}/0000-{_JOB}.wav"
    result = registry.delete(entry.asset_id)
    assert result.status is TempDeleteStatus.deleted
    assert not path.exists()
    assert registry.entries() == ()


def test_delete_failure_keeps_entry_and_uses_exponential_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import temp_assets

    paths = _paths(tmp_path)
    now = [100.0]
    registry = _registry(paths, now=now)
    path = _mock_wav(paths)
    entry = registry.register(path, TempAssetKind.mock_wav)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"occupied")
    original_delete = temp_assets._delete_without_following_reparse
    attempts = 0

    def fail_once(candidate: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("simulated antivirus file hold")
        original_delete(candidate)

    monkeypatch.setattr(temp_assets, "_delete_without_following_reparse", fail_once)

    first = registry.delete(entry.asset_id)
    assert first.status is TempDeleteStatus.pending
    assert first.retry_count == 1
    assert first.next_retry_at == 100.5
    assert path.exists()
    assert len(registry.entries()) == 1

    now[0] = 100.25
    too_early = registry.delete(entry.asset_id)
    assert too_early.status is TempDeleteStatus.pending
    assert attempts == 1

    now[0] = 100.5
    completed = registry.delete(entry.asset_id)
    assert completed.status is TempDeleteStatus.deleted
    assert attempts == 2
    assert registry.entries() == ()


def test_new_registry_scavenges_old_registered_crash_residue(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    before_crash = [100.0]
    first_process = _registry(paths, now=before_crash, minimum_age=60.0)
    path = _mock_wav(paths)
    first_process.register(path, TempAssetKind.mock_wav)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"crash residue")

    after_restart = [1_000.0]
    second_process = _registry(paths, now=after_restart, minimum_age=60.0)
    report = second_process.scavenge()

    assert report.deleted == 1
    assert not path.exists()
    assert second_process.entries() == ()


def test_two_registry_instances_serialize_concurrent_register_and_delete(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    first = _registry(paths)
    second = _registry(paths)
    candidates = [
        paths.temp / "audio" / "concurrent" / _TURN / f"{index:04d}-{index:020x}.wav"
        for index in range(32)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = tuple(
            executor.map(
                lambda pair: (first if pair[0] % 2 == 0 else second).register(
                    pair[1], TempAssetKind.tts_wav
                ),
                enumerate(candidates),
            )
        )

    assert len(first.entries()) == 32
    payload = json.loads(first.registry_path.read_text(encoding="ascii"))
    assert payload["format_version"] == 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(
            executor.map(
                lambda pair: (first if pair[0] % 2 == 0 else second).delete(pair[1].asset_id),
                enumerate(entries),
            )
        )

    assert all(result.status is TempDeleteStatus.missing for result in results)
    assert first.entries() == ()


def test_scavenger_discovers_only_old_strictly_named_wav_part_and_stt_directory(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    registry = _registry(paths, now=[1_000.0], minimum_age=100.0)
    registry.prepare()
    wav = _mock_wav(paths)
    part = wav.with_name(f".{wav.name}.{'c' * 32}.part")
    recording = paths.temp / "stt" / f"companion-recording-{'d' * 32}"
    invalid = paths.temp / "audio" / "mock" / "user-file.wav"
    for path in (wav, part, invalid):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"residue")
        os.utime(path, (100.0, 100.0))
    recording.mkdir(parents=True)
    (recording / "input.wav").write_bytes(b"recording")
    (recording / "transcript.json").write_text("{}", encoding="utf-8")
    os.utime(recording, (100.0, 100.0))

    report = registry.scavenge()

    assert report.discovered == 3
    assert report.deleted == 3
    assert not wav.exists()
    assert not part.exists()
    assert not recording.exists()
    assert invalid.exists(), "不符合严格命名的用户文件不得被启动扫描删除"


def test_corrupt_registry_with_parent_escape_fails_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    registry = _registry(paths)
    registry.prepare()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"keep")
    paths.state.joinpath("temp-assets.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "entries": [
                    {
                        "asset_id": f"tmp_{'e' * 32}",
                        "relative_path": "../outside.wav",
                        "kind": "tts_wav",
                        "created_at": 0,
                        "retry_count": 0,
                        "next_retry_at": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TempRegistryError, match="temp_registry_invalid_relative_path"):
        registry.scavenge()

    assert outside.read_bytes() == b"keep"


def test_register_rejects_path_outside_temp_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    registry = _registry(paths)

    with pytest.raises(TempRegistryError, match="temp_registry_path_escape"):
        registry.register(tmp_path / f"0000-{_JOB}.wav", TempAssetKind.tts_wav)


@pytest.mark.skipif(os.name != "nt", reason="Windows open handles deny file deletion")
def test_real_windows_occupied_handle_keeps_registry_entry(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    now = [100.0]
    registry = _registry(paths, now=now)
    path = _mock_wav(paths)
    entry = registry.register(path, TempAssetKind.mock_wav)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"occupied")

    with path.open("rb"):
        result = registry.delete(entry.asset_id)
        assert result.status is TempDeleteStatus.pending
        assert path.exists()
        assert len(registry.entries()) == 1

    now[0] = result.next_retry_at
    assert registry.delete(entry.asset_id).status is TempDeleteStatus.deleted
