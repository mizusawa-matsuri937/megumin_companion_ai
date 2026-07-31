"""Single-owner transactional model switching for the private TTS gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol, TypeVar

from app.tts_gateway.contracts import GatewayHealth, VoiceSlot
from app.tts_gateway.manifest import GatewayManifest, GatewayVoiceSlot

_ResultT = TypeVar("_ResultT")


class GatewayEngineErrorCode(StrEnum):
    unavailable = "tts_gateway_unavailable"
    slot_invalid = "tts_gateway_slot_invalid"
    switch_failed = "tts_gateway_switch_failed"
    inference_failed = "tts_gateway_inference_failed"
    quarantine = "tts_gateway_quarantine"
    audio_invalid = "tts_gateway_audio_invalid"


class GatewayEngineError(RuntimeError):
    """Stable inference failure without model, prompt, text, or path data."""

    def __init__(self, code: GatewayEngineErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class InferenceBackend(Protocol):
    """Blocking official inference adapter called only through one owned thread."""

    def load_initial(self, slot: GatewayVoiceSlot) -> None: ...

    def load_pair(self, slot: GatewayVoiceSlot) -> None: ...

    def synthesize(
        self,
        slot: GatewayVoiceSlot,
        text: str,
        *,
        speed_factor: float,
        batch_size: int,
    ) -> bytes: ...


class GatewayEngine:
    """Serialize inference and roll back both weights after any partial switch."""

    def __init__(
        self,
        manifest: GatewayManifest,
        backend: InferenceBackend,
        *,
        initial_slot: VoiceSlot = "neutral",
        wav_validator: Callable[[bytes], bool] | None = None,
    ) -> None:
        if initial_slot not in manifest.slots:
            raise GatewayEngineError(GatewayEngineErrorCode.slot_invalid)
        self._manifest = manifest
        self._backend = backend
        self._initial_slot = initial_slot
        self._current_slot: VoiceSlot | None = None
        self._quarantined = False
        self._lock = asyncio.Lock()
        self._wav_validator = wav_validator or _valid_wav_header

    @property
    def quarantined(self) -> bool:
        return self._quarantined

    @property
    def current_slot(self) -> VoiceSlot | None:
        return self._current_slot

    async def start(self) -> None:
        async with self._lock:
            if self._quarantined:
                raise GatewayEngineError(GatewayEngineErrorCode.quarantine)
            if self._current_slot is not None:
                return
            try:
                await _run_owned_thread(
                    self._backend.load_initial,
                    self._manifest.slots[self._initial_slot],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._quarantined = True
                raise GatewayEngineError(GatewayEngineErrorCode.unavailable) from exc
            self._current_slot = self._initial_slot

    async def synthesize(
        self,
        voice_slot: VoiceSlot,
        text: str,
        *,
        speed_factor: float,
    ) -> bytes:
        if voice_slot not in self._manifest.slots:
            raise GatewayEngineError(GatewayEngineErrorCode.slot_invalid)
        async with self._lock:
            if self._quarantined:
                raise GatewayEngineError(GatewayEngineErrorCode.quarantine)
            if self._current_slot is None:
                raise GatewayEngineError(GatewayEngineErrorCode.unavailable)
            if voice_slot != self._current_slot:
                await self._switch(voice_slot)
            selected = self._manifest.slots[voice_slot]
            try:
                audio = await _run_owned_thread(
                    self._backend.synthesize,
                    selected,
                    text,
                    speed_factor=speed_factor,
                    batch_size=self._manifest.batch_size,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise GatewayEngineError(GatewayEngineErrorCode.inference_failed) from exc
            if not self._wav_validator(audio):
                raise GatewayEngineError(GatewayEngineErrorCode.audio_invalid)
            return audio

    def health(self) -> GatewayHealth:
        return GatewayHealth(
            status="quarantine" if self._quarantined else "ready",
            current_slot=self._current_slot,
        )

    async def _switch(self, target: VoiceSlot) -> None:
        previous = self._current_slot
        assert previous is not None
        try:
            await _run_owned_thread(
                self._backend.load_pair,
                self._manifest.slots[target],
            )
        except asyncio.CancelledError:
            await self._rollback_or_quarantine(previous)
            raise
        except Exception as exc:
            try:
                await self._rollback_or_quarantine(previous)
            except GatewayEngineError:
                raise
            raise GatewayEngineError(GatewayEngineErrorCode.switch_failed) from exc
        self._current_slot = target

    async def _rollback_or_quarantine(self, previous: VoiceSlot) -> None:
        try:
            await _run_owned_thread(
                self._backend.load_pair,
                self._manifest.slots[previous],
            )
        except asyncio.CancelledError:
            self._quarantined = True
            self._current_slot = None
            raise GatewayEngineError(GatewayEngineErrorCode.quarantine) from None
        except Exception as exc:
            self._quarantined = True
            self._current_slot = None
            raise GatewayEngineError(GatewayEngineErrorCode.quarantine) from exc
        self._current_slot = previous


async def _run_owned_thread(
    function: Callable[..., _ResultT],
    /,
    *args: object,
    **kwargs: object,
) -> _ResultT:
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(operation)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            cancelled = True
            if operation.done():
                await asyncio.gather(operation, return_exceptions=True)
                raise


def _valid_wav_header(value: bytes) -> bool:
    return (
        isinstance(value, bytes)
        and len(value) >= 44
        and value[:4] == b"RIFF"
        and value[8:12] == b"WAVE"
    )
