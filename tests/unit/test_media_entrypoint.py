"""Unit coverage for MediaWorker's private process entry point."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import app.media_entrypoint as media_entrypoint
import pytest
from app.media.stt import WhisperCppConfig
from app.workers.access import ResourceAccessError


def test_root_argument_accepts_absolute_roots_and_rejects_invalid_values(tmp_path: Path) -> None:
    root_id, root = media_entrypoint._root_argument(f"audio_temp={tmp_path}")

    assert root_id == "audio_temp"
    assert root == tmp_path
    for value in ("missing_separator", "INVALID=/tmp", "audio_temp=relative"):
        with pytest.raises(Exception, match="invalid root"):
            media_entrypoint._root_argument(value)


def test_media_entrypoint_builds_private_runtime_from_validated_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class FakePolicy:
        def __init__(self, *, roots: dict[str, Path]) -> None:
            captured["roots"] = roots

    class FakeHandler:
        def __init__(self, *, selected_device_id: str | None, maximum_wave_bytes: int) -> None:
            captured["selected_device_id"] = selected_device_id
            captured["maximum_wave_bytes"] = maximum_wave_bytes

    class FakeRuntime:
        def __init__(
            self,
            *,
            role: str,
            handler: FakeHandler,
            resource_policy: FakePolicy,
            maximum_active_jobs: int,
        ) -> None:
            captured["role"] = role
            captured["handler"] = handler
            captured["policy"] = resource_policy
            captured["maximum_active_jobs"] = maximum_active_jobs

        async def run(self) -> int:
            return 17

    monkeypatch.setattr(media_entrypoint, "ApprovedResourcePolicy", FakePolicy)
    monkeypatch.setattr(media_entrypoint, "MediaWorkerHandler", FakeHandler)
    monkeypatch.setattr(media_entrypoint, "HelperRuntime", FakeRuntime)
    output_device_id = "audio_" + "a" * 32
    argv = [
        "--root",
        f"audio_temp={tmp_path}",
        "--output-device-id",
        output_device_id,
        "--maximum-wave-bytes",
        "2048",
    ]

    assert media_entrypoint.main(argv) == 17
    assert captured["roots"] == {"audio_temp": tmp_path}
    assert captured["selected_device_id"] == output_device_id
    assert captured["maximum_wave_bytes"] == 2048
    assert captured["role"] == "media"
    assert captured["maximum_active_jobs"] == 1


def test_media_entrypoint_rejects_duplicate_or_invalid_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path.cwd()
    executable = tmp_path / "whisper.exe"
    model = tmp_path / "model.bin"
    assert (
        asyncio.run(
            media_entrypoint.run(["--root", f"audio_temp={root}", "--root", f"audio_temp={root}"])
        )
        == 2
    )
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    assert (
        asyncio.run(
            media_entrypoint.run(
                [
                    "--root",
                    f"stt_temp={root}",
                    "--stt-executable",
                    str(executable),
                    "--stt-model",
                    str(model),
                    "--stt-temporary-root",
                    str(outside_root),
                ]
            )
        )
        == 2
    )
    assert (
        asyncio.run(
            media_entrypoint.run(
                [
                    "--root",
                    f"stt_temp={root}",
                    "--stt-executable",
                    str(executable),
                    "--stt-model",
                    str(model),
                    "--stt-temporary-root",
                    str(root),
                    "--stt-language",
                    "en",
                ]
            )
        )
        == 2
    )
    assert (
        asyncio.run(
            media_entrypoint.run(
                ["--root", f"audio_temp={root}", "--output-device-id", "not-a-device"]
            )
        )
        == 2
    )

    class FailingPolicy:
        def __init__(self, **_options: object) -> None:
            raise ResourceAccessError("worker_approved_root_invalid")

    monkeypatch.setattr(media_entrypoint, "ApprovedResourcePolicy", FailingPolicy)
    assert asyncio.run(media_entrypoint.run(["--root", f"audio_temp={root}"])) == 2


def test_media_entrypoint_passes_only_complete_stt_configuration_to_media_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class FakePolicy:
        def __init__(self, **_options: object) -> None:
            return None

    class FakeHandler:
        def __init__(self, **options: object) -> None:
            captured.update(options)

    class FakeRuntime:
        def __init__(self, **_options: object) -> None:
            return None

        async def run(self) -> int:
            return 23

    root = tmp_path / "私有临时根"
    root.mkdir()
    executable = tmp_path / "中文 whisper.exe"
    model = tmp_path / "模型.bin"
    monkeypatch.setattr(media_entrypoint, "ApprovedResourcePolicy", FakePolicy)
    monkeypatch.setattr(media_entrypoint, "MediaWorkerHandler", FakeHandler)
    monkeypatch.setattr(media_entrypoint, "HelperRuntime", FakeRuntime)

    assert (
        media_entrypoint.main(
            [
                "--root",
                f"stt_temp={root}",
                "--stt-executable",
                str(executable),
                "--stt-model",
                str(model),
                "--stt-temporary-root",
                str(root),
                "--stt-executable-prefix=--portable",
                "--stt-language",
                "zh",
                "--stt-threads",
                "2",
                "--stt-maximum-recording-seconds",
                "120",
                "--stt-transcription-timeout-seconds",
                "30",
                "--input-device-name",
                "Synthetic microphone",
            ]
        )
        == 23
    )
    config = captured["stt_config"]
    assert isinstance(config, WhisperCppConfig)
    assert config.executable == executable
    assert config.model_path == model
    assert config.executable_prefix_args == ("--portable",)
    assert captured["recording_root"] == root
    assert captured["input_device"] == "Synthetic microphone"
    assert captured["maximum_recording_seconds"] == 120.0
    assert captured["transcription_timeout_seconds"] == 30.0

    assert (
        asyncio.run(
            media_entrypoint.run(
                [
                    "--root",
                    f"stt_temp={root}",
                    "--stt-executable",
                    str(executable),
                ]
            )
        )
        == 2
    )
    assert (
        asyncio.run(
            media_entrypoint.run(
                [
                    "--root",
                    f"stt_temp={root}",
                    "--stt-executable",
                    str(executable),
                    "--stt-model",
                    str(model),
                    "--stt-temporary-root",
                    str(root),
                    "--input-device-index",
                    "-1",
                ]
            )
        )
        == 2
    )
