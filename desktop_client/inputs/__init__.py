"""Explicit local voice-input composition and contracts."""

from desktop_client.inputs.factory import build_stt_provider, build_voice_input
from desktop_client.inputs.stt_contracts import (
    STTError,
    STTErrorCode,
    STTProvider,
    TranscriptionRequest,
    TranscriptionResult,
)
from desktop_client.inputs.voice_input import (
    PushToTalkConfig,
    PushToTalkRecorder,
    RecordingState,
    SoundDevicePCMInput,
    VoiceInputError,
    VoiceInputErrorCode,
)
from desktop_client.inputs.whisper_cpp import WhisperCppConfig, WhisperCppProvider

__all__ = [
    "PushToTalkConfig",
    "PushToTalkRecorder",
    "RecordingState",
    "STTError",
    "STTErrorCode",
    "STTProvider",
    "SoundDevicePCMInput",
    "TranscriptionRequest",
    "TranscriptionResult",
    "VoiceInputError",
    "VoiceInputErrorCode",
    "WhisperCppConfig",
    "WhisperCppProvider",
    "build_stt_provider",
    "build_voice_input",
]
