"""Ordered-playback contracts and safe non-native fallback backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.cancellation import CancellationToken
from app.schemas import AudioResult


class AudioPlayer(Protocol):
    async def play(self, result: AudioResult, token: CancellationToken) -> AudioPlaybackResult: ...

    async def stop(self, *, immediate: bool = False) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AudioPlaybackResult:
    """A body-free playback outcome consumed by the bounded dialogue pipeline.

    A completed fallback still has ``played=True`` and carries a stable notice
    code.  A device loss has ``played=False`` so text can continue without
    emitting a false ``playback.finished`` event.
    """

    played: bool
    error_code: str | None = None
    notice_code: str | None = None


class SilentAudioPlayer:
    """Model playback duration without opening an audio device."""

    def __init__(self, *, realtime: bool = False) -> None:
        self._realtime = realtime

    async def play(self, result: AudioResult, token: CancellationToken) -> AudioPlaybackResult:
        duration = (result.duration_ms or 0) / 1000 if self._realtime else 0.0
        if await token.wait_or_timeout(duration):
            token.raise_if_cancelled()
        return AudioPlaybackResult(played=result.success)

    async def stop(self, *, immediate: bool = False) -> None:
        return None

    async def close(self) -> None:
        return None
