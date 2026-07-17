"""STT configuration and lazy composition tests."""

from pathlib import Path

import pytest
from app.config import Settings
from app.config.settings import STTConfig
from desktop_client.inputs import (
    PushToTalkRecorder,
    SoundDevicePCMInput,
    WhisperCppProvider,
    build_stt_provider,
)
from desktop_client.inputs.factory import build_voice_input


def test_disabled_stt_builds_nothing_and_never_imports_sounddevice() -> None:
    settings = Settings(stt=STTConfig(enabled=False, provider="unsupported"))

    assert build_stt_provider(settings) is None
    assert build_voice_input(settings) is None


def test_enabled_whisper_cpp_factory_resolves_paths_without_opening_microphone() -> None:
    settings = Settings(
        stt=STTConfig(
            enabled=True,
            provider=" whisper-cpp ",
            executable=Path("runtime/whisper-cli"),
            model_path=Path("models/local.bin"),
            temporary_directory=Path("data/private/test-stt"),
            threads=4,
            max_recording_seconds=3,
            transcription_timeout_seconds=7,
            device="test-device",
            blocksize=320,
        )
    )

    recorder = build_voice_input(settings)

    assert isinstance(recorder, PushToTalkRecorder)
    assert isinstance(recorder._stt, WhisperCppProvider)
    assert recorder._stt._config.executable == settings.runtime_base / "runtime/whisper-cli"
    assert recorder._stt._config.model_path == settings.runtime_base / "models/local.bin"
    assert recorder._stt._config.threads == 4
    assert recorder._config.max_recording_seconds == 3
    assert recorder._config.transcription_timeout_seconds == 7
    assert isinstance(recorder._source, SoundDevicePCMInput)
    assert recorder._source._device == "test-device"
    assert recorder._source._blocksize == 320


def test_stt_factory_preserves_absolute_paths_and_rejects_unknown_provider(
    tmp_path: Path,
) -> None:
    provider = build_stt_provider(
        Settings(
            stt=STTConfig(
                enabled=True,
                executable=tmp_path / "whisper-cli",
                model_path=tmp_path / "model.bin",
                temporary_directory=tmp_path / "scratch",
            )
        )
    )
    assert isinstance(provider, WhisperCppProvider)
    assert provider._config.executable == tmp_path / "whisper-cli"
    assert provider._config.temporary_directory == tmp_path / "scratch"

    with pytest.raises(RuntimeError, match="不支持"):
        build_stt_provider(Settings(stt=STTConfig(enabled=True, provider="remote")))


@pytest.mark.parametrize(
    "options",
    [
        {"provider": " "},
        {"language": " "},
        {"threads": 0},
        {"max_recording_seconds": 0},
        {"blocksize": -1},
    ],
)
def test_stt_settings_reject_invalid_bounds(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        STTConfig(**options)  # type: ignore[arg-type]
