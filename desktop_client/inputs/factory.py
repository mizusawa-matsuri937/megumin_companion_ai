"""Build local STT components without opening a microphone or downloading models."""

from __future__ import annotations

from app.config import Settings
from app.temp_assets import TempAssetRegistry

from desktop_client.inputs.stt_contracts import STTProvider
from desktop_client.inputs.voice_input import (
    PushToTalkConfig,
    PushToTalkRecorder,
    SoundDevicePCMInput,
)
from desktop_client.inputs.whisper_cpp import WhisperCppConfig, WhisperCppProvider


def build_stt_provider(
    settings: Settings,
    *,
    temp_registry: TempAssetRegistry | None = None,
) -> STTProvider | None:
    """Return the explicitly enabled local provider, never a mock fallback."""

    if not settings.stt.enabled:
        return None
    provider_name = settings.stt.provider.strip().lower().replace("-", "_")
    if provider_name != "whisper_cpp":
        raise RuntimeError(f"不支持的 STT provider：{settings.stt.provider}")
    return WhisperCppProvider(
        WhisperCppConfig(
            executable=settings.stt_executable_path(),
            model_path=settings.stt_model_path(),
            threads=settings.stt.threads,
            temporary_directory=settings.stt_temporary_directory(),
            terminate_grace_seconds=settings.stt.terminate_grace_seconds,
            max_audio_bytes=settings.stt.max_audio_bytes,
            max_output_bytes=settings.stt.max_output_bytes,
        ),
        temp_registry=temp_registry,
    )


def build_voice_input(
    settings: Settings,
    *,
    temp_registry: TempAssetRegistry | None = None,
) -> PushToTalkRecorder | None:
    """Compose push-to-talk input; the microphone remains closed until ``start``."""

    provider = build_stt_provider(settings, temp_registry=temp_registry)
    if provider is None:
        return None
    return PushToTalkRecorder(
        SoundDevicePCMInput(device=settings.stt.device, blocksize=settings.stt.blocksize),
        provider,
        config=PushToTalkConfig(
            max_recording_seconds=settings.stt.max_recording_seconds,
            transcription_timeout_seconds=settings.stt.transcription_timeout_seconds,
            language=settings.stt.language,
            temporary_directory=settings.stt_temporary_directory(),
        ),
        temp_registry=temp_registry,
    )
