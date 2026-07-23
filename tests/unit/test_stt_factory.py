"""W18 factory tests: PTT construction is inert until an explicit start."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from app.config import Settings
from app.config.settings import STTConfig
from app.media.voice import MediaWorkerVoiceInput, VoiceCaptureError
from app.paths import AppPaths
from app.temp_assets import TempAssetRegistry
from app.windows_security import PortableDirectorySecurity
from desktop_client.inputs import PushToTalkRecorder, build_voice_input


def test_disabled_stt_builds_nothing_and_never_imports_worker_sounddevice() -> None:
    settings = Settings(stt=STTConfig(enabled=False, provider="unsupported"))
    before = "app.media.worker" in sys.modules

    assert build_voice_input(settings) is None

    assert ("app.media.worker" in sys.modules) is before


def test_enabled_factory_resolves_private_worker_roots_without_opening_microphone(
    tmp_path: Path,
) -> None:
    settings = Settings(
        stt=STTConfig(
            enabled=True,
            provider=" whisper-cpp ",
            executable=Path("runtime/whisper-cli.exe"),
            model_path=Path("models/local.bin"),
            temporary_directory=Path("test-stt"),
            threads=4,
            max_recording_seconds=3,
            transcription_timeout_seconds=7,
            device="test-device",
            blocksize=320,
        )
    )
    settings._paths = AppPaths(root=tmp_path / "AppData")
    registry = TempAssetRegistry(
        settings.paths,
        directory_security=PortableDirectorySecurity(),
    )

    recorder = build_voice_input(settings, temp_registry=registry)

    assert isinstance(recorder, PushToTalkRecorder)
    assert isinstance(recorder._capture, MediaWorkerVoiceInput)
    assert recorder._capture.state.value == "idle"
    assert recorder._capture._supervisor is None
    assert recorder._capture._roots["stt_temp"] == settings.paths.temp / "test-stt"
    assert recorder._capture._temp_registry is registry
    assert not (settings.paths.temp / "test-stt").exists()


def test_factory_rejects_unknown_enabled_provider_with_stable_code() -> None:
    with pytest.raises(VoiceCaptureError) as caught:
        build_voice_input(Settings(stt=STTConfig(enabled=True, provider="remote")))
    assert caught.value.code == "stt_provider_unsupported"


def test_factory_maps_invalid_stt_paths_to_a_body_free_capability_error() -> None:
    with pytest.raises(VoiceCaptureError) as caught:
        build_voice_input(
            Settings(
                stt=STTConfig(
                    enabled=True,
                    temporary_directory=Path("../outside-private-root"),
                )
            )
        )
    assert caught.value.code == "stt_config_invalid"


def test_stt_configuration_normalizes_legacy_auto_to_chinese_only() -> None:
    assert STTConfig().language == "zh"
    assert STTConfig().managed_profile == "whispercpp_base_q5_1"
    assert STTConfig(language="auto").language == "zh"
    assert STTConfig(language="zh-CN").language == "zh"
    with pytest.raises(ValueError, match="仅支持中文"):
        STTConfig(language="ja")


@pytest.mark.parametrize(
    "options",
    [
        {"provider": " "},
        {"provider": "\x00"},
        {"managed_profile": "whispercpp_unreviewed_large"},
        {"language": " "},
        {"language": "\x00"},
        {"language": "x" * 65},
        {"threads": 0},
        {"max_recording_seconds": 0},
        {"max_recording_seconds": 120.1},
        {"transcription_timeout_seconds": 120.1},
        {"device": True},
        {"device": -1},
        {"device": "\x00"},
        {"device": "x" * 257},
        {"blocksize": -1},
    ],
)
def test_stt_settings_reject_invalid_w18_bounds(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        STTConfig(**options)  # type: ignore[arg-type]
