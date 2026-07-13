"""Private contracts for local speech-to-text input adapters.

These types deliberately contain no raw audio payload.  Audio is passed to an
STT provider by a short-lived local path and the caller remains responsible for
deleting it on every exit path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class STTErrorCode(StrEnum):
    executable_missing = "stt_executable_missing"
    model_missing = "stt_model_missing"
    invalid_audio = "stt_invalid_audio"
    process_start_failed = "stt_process_start_failed"
    process_failed = "stt_process_failed"
    timeout = "stt_timeout"
    invalid_response = "stt_invalid_response"
    empty_transcript = "stt_empty_transcript"
    output_too_large = "stt_output_too_large"
    closed = "stt_closed"


class STTError(RuntimeError):
    """A privacy-safe, stable failure from a local STT provider."""

    def __init__(self, code: STTErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    audio_path: Path
    language: str = "auto"
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise ValueError("language 不能为空")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    language: str | None = None
    segment_count: int = 0

    def __post_init__(self) -> None:
        normalized = self.text.strip()
        if not normalized:
            raise ValueError("转写文本不能为空")
        object.__setattr__(self, "text", normalized)
        if self.segment_count < 0:
            raise ValueError("segment_count 不能为负数")


class STTProvider(Protocol):
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...

    async def close(self) -> None: ...
