"""Build local STT components without opening a microphone or downloading models."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.config.settings import PROJECT_ROOT

from desktop_client.inputs.stt_contracts import STTProvider
from desktop_client.inputs.voice_input import (
    PushToTalkConfig,
    PushToTalkRecorder,
    SoundDevicePCMInput,
)
from desktop_client.inputs.whisper_cpp import WhisperCppConfig, WhisperCppProvider


def build_stt_provider(settings: Settings) -> STTProvider | None:
    """Return the explicitly enabled local provider, never a mock fallback."""

    if not settings.stt.enabled:
        return None
    provider_name = settings.stt.provider.strip().lower().replace("-", "_")
    if provider_name != "whisper_cpp":
        raise RuntimeError(f"不支持的 STT provider：{settings.stt.provider}")
    return WhisperCppProvider(
        WhisperCppConfig(
            executable=_project_path(settings.stt.executable),
            model_path=_project_path(settings.stt.model_path),
            threads=settings.stt.threads,
            temporary_directory=_project_path(settings.stt.temporary_directory),
            terminate_grace_seconds=settings.stt.terminate_grace_seconds,
            max_audio_bytes=settings.stt.max_audio_bytes,
            max_output_bytes=settings.stt.max_output_bytes,
        )
    )


def build_voice_input(settings: Settings) -> PushToTalkRecorder | None:
    """Compose push-to-talk input; the microphone remains closed until ``start``."""

    provider = build_stt_provider(settings)
    if provider is None:
        return None
    return PushToTalkRecorder(
        SoundDevicePCMInput(device=settings.stt.device, blocksize=settings.stt.blocksize),
        provider,
        config=PushToTalkConfig(
            max_recording_seconds=settings.stt.max_recording_seconds,
            transcription_timeout_seconds=settings.stt.transcription_timeout_seconds,
            language=settings.stt.language,
            temporary_directory=_project_path(settings.stt.temporary_directory),
        ),
    )


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path
