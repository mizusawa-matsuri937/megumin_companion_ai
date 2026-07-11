"""Provider-neutral TTS contract."""

from __future__ import annotations

from typing import Protocol

from app.core.cancellation import CancellationToken
from app.schemas import AudioResult, TTSJob


class TTSProvider(Protocol):
    async def synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult: ...

    async def discard(self, result: AudioResult) -> None: ...

    async def close(self) -> None: ...
