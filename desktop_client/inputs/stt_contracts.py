"""Compatibility names for the bounded transcript-only desktop contract."""

from app.media.voice import VoiceCaptureError as STTError
from app.media.voice import VoiceTranscription as TranscriptionResult

__all__ = ["STTError", "TranscriptionResult"]
