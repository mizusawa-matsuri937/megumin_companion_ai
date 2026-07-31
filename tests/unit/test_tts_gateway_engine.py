from __future__ import annotations

import asyncio
import io
import threading
import time
import wave
from typing import cast

import pytest
from app.tts_gateway.contracts import VoiceSlot
from app.tts_gateway.engine import (
    GatewayEngine,
    GatewayEngineError,
    GatewayEngineErrorCode,
    InferenceBackend,
)
from app.tts_gateway.manifest import (
    FileDigest,
    GatewayCommonAssets,
    GatewayManifest,
    GatewayVoiceSlot,
    TreeDigest,
)


def _wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(32_000)
        target.writeframes(b"\x00\x00" * 320)
    return output.getvalue()


def _slot(name: str) -> GatewayVoiceSlot:
    return GatewayVoiceSlot(
        gpt_weight=FileDigest(path=f"{name}/gpt.ckpt", sha256="a" * 64),
        sovits_weight=FileDigest(path=f"{name}/sovits.pth", sha256="b" * 64),
        reference_audio=FileDigest(path=f"{name}/reference.wav", sha256="c" * 64),
        prompt_text="参照文",
        prompt_lang="ja",
        text_lang="zh",
        archive_sha256="d" * 64,
    )


def _manifest() -> GatewayManifest:
    slot_names: tuple[VoiceSlot, ...] = (
        "neutral",
        "gentle",
        "tsundere",
        "focused",
        "excited_explosion",
    )
    slots = {name: _slot(name) for name in slot_names}
    return GatewayManifest(
        format_version=1,
        source_commit="d523079fc05d9a8028d6085bffe4a2757c32abb6",
        source_root="source",
        source_tree_sha256="e" * 64,
        common=GatewayCommonAssets(
            bert=TreeDigest(path="bert", sha256="f" * 64),
            cnhubert=TreeDigest(path="hubert", sha256="0" * 64),
            ffmpeg_bin=TreeDigest(path="ffmpeg", sha256="3" * 64),
            g2pw=TreeDigest(path="source/GPT_SoVITS/text/G2PWModel", sha256="2" * 64),
            language_detection=TreeDigest(
                path="source/GPT_SoVITS/pretrained_models/fast_langdetect",
                sha256="4" * 64,
            ),
            open_jtalk=TreeDigest(path="open-jtalk", sha256="5" * 64),
            speaker_verification=FileDigest(path="sv.ckpt", sha256="1" * 64),
        ),
        batch_size=20,
        slots=slots,
        device="cuda",
        is_half=True,
    )


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_targets: set[str] = set()
        self.fail_initial = False
        self.fail_inference = False
        self.audio = _wav()
        self.maximum_active = 0
        self._active = 0
        self._guard = threading.Lock()

    def _name(self, slot: GatewayVoiceSlot) -> str:
        return slot.gpt_weight.path.split("/", 1)[0]

    def load_initial(self, slot: GatewayVoiceSlot) -> None:
        self.calls.append(("initial", self._name(slot)))
        if self.fail_initial:
            raise RuntimeError("private initial failure")

    def load_pair(self, slot: GatewayVoiceSlot) -> None:
        name = self._name(slot)
        self.calls.append(("load", name))
        if name in self.fail_targets:
            raise RuntimeError("private failure detail")

    def synthesize(
        self,
        slot: GatewayVoiceSlot,
        text: str,
        *,
        speed_factor: float,
        batch_size: int,
    ) -> bytes:
        del text, speed_factor, batch_size
        name = self._name(slot)
        if self.fail_inference:
            raise RuntimeError("private inference failure")
        with self._guard:
            self._active += 1
            self.maximum_active = max(self.maximum_active, self._active)
        try:
            time.sleep(0.02)
            self.calls.append(("tts", name))
            return self.audio
        finally:
            with self._guard:
                self._active -= 1


def test_engine_starts_neutral_and_serializes_switch_plus_inference() -> None:
    async def scenario() -> None:
        backend = _Backend()
        engine = GatewayEngine(_manifest(), cast(InferenceBackend, backend))
        await engine.start()

        first, second = await asyncio.gather(
            engine.synthesize("gentle", "第一段", speed_factor=1.05),
            engine.synthesize("focused", "第二段", speed_factor=0.95),
        )

        assert first[:4] == second[:4] == b"RIFF"
        assert backend.calls[0] == ("initial", "neutral")
        assert backend.maximum_active == 1
        assert engine.current_slot == "focused"

    asyncio.run(scenario())


def test_partial_switch_failure_rolls_back_both_weights() -> None:
    async def scenario() -> None:
        backend = _Backend()
        engine = GatewayEngine(_manifest(), cast(InferenceBackend, backend))
        await engine.start()
        backend.fail_targets.add("gentle")

        with pytest.raises(GatewayEngineError) as caught:
            await engine.synthesize("gentle", "失败", speed_factor=1.0)

        assert caught.value.code is GatewayEngineErrorCode.switch_failed
        assert backend.calls[-2:] == [("load", "gentle"), ("load", "neutral")]
        assert engine.current_slot == "neutral"
        assert not engine.quarantined
        assert "private failure detail" not in str(caught.value)

    asyncio.run(scenario())


def test_rollback_failure_enters_quarantine() -> None:
    async def scenario() -> None:
        backend = _Backend()
        engine = GatewayEngine(_manifest(), cast(InferenceBackend, backend))
        await engine.start()
        backend.fail_targets.update({"gentle", "neutral"})

        with pytest.raises(GatewayEngineError) as caught:
            await engine.synthesize("gentle", "失败", speed_factor=1.0)

        assert caught.value.code is GatewayEngineErrorCode.quarantine
        assert engine.quarantined
        assert engine.current_slot is None
        assert engine.health().status == "quarantine"

    asyncio.run(scenario())


def test_engine_rejects_invalid_slots_unstarted_and_backend_failures() -> None:
    backend = _Backend()
    with pytest.raises(GatewayEngineError) as invalid_initial:
        GatewayEngine(
            _manifest(),
            cast(InferenceBackend, backend),
            initial_slot=cast(VoiceSlot, "unknown"),
        )
    assert invalid_initial.value.code is GatewayEngineErrorCode.slot_invalid

    async def scenario() -> None:
        unstarted = GatewayEngine(_manifest(), cast(InferenceBackend, _Backend()))
        with pytest.raises(GatewayEngineError) as invalid_slot:
            await unstarted.synthesize(
                cast(VoiceSlot, "unknown"),
                "正文",
                speed_factor=1.0,
            )
        assert invalid_slot.value.code is GatewayEngineErrorCode.slot_invalid
        with pytest.raises(GatewayEngineError) as unavailable:
            await unstarted.synthesize("neutral", "正文", speed_factor=1.0)
        assert unavailable.value.code is GatewayEngineErrorCode.unavailable

        initial_failure = _Backend()
        initial_failure.fail_initial = True
        failed = GatewayEngine(_manifest(), cast(InferenceBackend, initial_failure))
        with pytest.raises(GatewayEngineError) as start_error:
            await failed.start()
        assert start_error.value.code is GatewayEngineErrorCode.unavailable
        assert failed.quarantined
        with pytest.raises(GatewayEngineError) as quarantined_start:
            await failed.start()
        assert quarantined_start.value.code is GatewayEngineErrorCode.quarantine
        with pytest.raises(GatewayEngineError) as quarantined_inference:
            await failed.synthesize("neutral", "正文", speed_factor=1.0)
        assert quarantined_inference.value.code is GatewayEngineErrorCode.quarantine

        inference_failure = _Backend()
        engine = GatewayEngine(_manifest(), cast(InferenceBackend, inference_failure))
        await engine.start()
        await engine.start()
        assert inference_failure.calls.count(("initial", "neutral")) == 1
        inference_failure.fail_inference = True
        with pytest.raises(GatewayEngineError) as inference:
            await engine.synthesize("neutral", "正文", speed_factor=1.0)
        assert inference.value.code is GatewayEngineErrorCode.inference_failed

        inference_failure.fail_inference = False
        inference_failure.audio = b"not-a-wave"
        with pytest.raises(GatewayEngineError) as audio:
            await engine.synthesize("neutral", "正文", speed_factor=1.0)
        assert audio.value.code is GatewayEngineErrorCode.audio_invalid

    asyncio.run(scenario())


class _CancelledSwitchBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.switch_started = threading.Event()
        self.release = threading.Event()

    def load_pair(self, slot: GatewayVoiceSlot) -> None:
        name = self._name(slot)
        self.calls.append(("load", name))
        if name == "gentle":
            self.switch_started.set()
            self.release.wait(timeout=1)


class _CancelledRollbackBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.rollback_started = threading.Event()
        self.release = threading.Event()

    def load_pair(self, slot: GatewayVoiceSlot) -> None:
        name = self._name(slot)
        self.calls.append(("load", name))
        if name == "gentle":
            raise RuntimeError("private switch failure")
        self.rollback_started.set()
        self.release.wait(timeout=1)


def test_engine_cancellation_rolls_back_or_quarantines_transactionally() -> None:
    async def scenario() -> None:
        backend = _CancelledSwitchBackend()
        engine = GatewayEngine(_manifest(), cast(InferenceBackend, backend))
        await engine.start()
        switching = asyncio.create_task(engine.synthesize("gentle", "正文", speed_factor=1.0))
        assert await asyncio.to_thread(backend.switch_started.wait, 1)
        switching.cancel()
        backend.release.set()
        with pytest.raises(asyncio.CancelledError):
            await switching
        assert engine.current_slot == "neutral"
        assert not engine.quarantined

        rollback = _CancelledRollbackBackend()
        quarantined = GatewayEngine(_manifest(), cast(InferenceBackend, rollback))
        await quarantined.start()
        switching = asyncio.create_task(quarantined.synthesize("gentle", "正文", speed_factor=1.0))
        assert await asyncio.to_thread(rollback.rollback_started.wait, 1)
        switching.cancel()
        rollback.release.set()
        with pytest.raises(GatewayEngineError) as caught:
            await switching
        assert caught.value.code is GatewayEngineErrorCode.quarantine
        assert quarantined.quarantined
        assert quarantined.current_slot is None

    asyncio.run(scenario())
