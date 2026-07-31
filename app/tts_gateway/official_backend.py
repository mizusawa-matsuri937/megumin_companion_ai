"""Pinned GPT-SoVITS v2ProPlus adapter loaded only by the private gateway."""

from __future__ import annotations

import importlib
import io
import os
import sys
import wave
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

from app.tts_gateway.manifest import GatewayManifest, GatewayVoiceSlot


class _PCMArray(Protocol):
    ndim: int
    size: int
    dtype: object

    def tobytes(self) -> bytes: ...


class OfficialGPTSoVITSBackend:
    """Blocking adapter around the pinned official ``TTS`` implementation."""

    def __init__(self, manifest: GatewayManifest, root: Path) -> None:
        self._manifest = manifest
        self._root = root
        self._source_root = _resolve(root, manifest.source_root)
        self._module: ModuleType | None = None
        self._tts: Any | None = None
        self._speed_change: Any | None = None

    def load_initial(self, slot: GatewayVoiceSlot) -> None:
        if self._tts is not None:
            return
        with _silence_official_output():
            module = self._import_official()
            config = module.TTS_Config(
                {
                    "custom": {
                        "device": self._manifest.device,
                        "is_half": self._manifest.is_half,
                        "version": "v2ProPlus",
                        "t2s_weights_path": str(_resolve(self._root, slot.gpt_weight.path)),
                        "vits_weights_path": str(_resolve(self._root, slot.sovits_weight.path)),
                        "bert_base_path": str(
                            _resolve(self._root, self._manifest.common.bert.path)
                        ),
                        "cnhuhbert_base_path": str(
                            _resolve(self._root, self._manifest.common.cnhubert.path)
                        ),
                    }
                }
            )
            self._tts = module.TTS(config)
        if getattr(
            self._tts.configs, "version", None
        ) != "v2ProPlus" or not self._loaded_pair_matches(slot):
            self._tts = None
            raise RuntimeError("official model load verification failed")

    def load_pair(self, slot: GatewayVoiceSlot) -> None:
        if self._tts is None:
            raise RuntimeError("official backend is not initialized")
        with _silence_official_output():
            self._tts.init_t2s_weights(str(_resolve(self._root, slot.gpt_weight.path)))
            self._tts.init_vits_weights(str(_resolve(self._root, slot.sovits_weight.path)))
        if getattr(
            self._tts.configs, "version", None
        ) != "v2ProPlus" or not self._loaded_pair_matches(slot):
            raise RuntimeError("official model load verification failed")

    def synthesize(
        self,
        slot: GatewayVoiceSlot,
        text: str,
        *,
        speed_factor: float,
        batch_size: int,
    ) -> bytes:
        if self._tts is None:
            raise RuntimeError("official backend is not initialized")
        with _silence_official_output():
            output = tuple(
                self._tts.run(
                    {
                        "text": text,
                        "text_lang": slot.text_lang,
                        "ref_audio_path": str(_resolve(self._root, slot.reference_audio.path)),
                        "aux_ref_audio_paths": [],
                        "prompt_text": slot.prompt_text,
                        "prompt_lang": slot.prompt_lang,
                        "top_k": 5,
                        "top_p": 1.0,
                        "temperature": 1.0,
                        "text_split_method": "cut1",
                        "batch_size": batch_size,
                        "batch_threshold": 0.75,
                        "split_bucket": True,
                        # Official code disables bucketing for non-unit speed.
                        # Preserve bucketed inference and apply its own ffmpeg
                        # speed helper to the final PCM instead.
                        "speed_factor": 1.0,
                        "fragment_interval": 0.3,
                        "seed": -1,
                        "parallel_infer": True,
                        "repetition_penalty": 1.35,
                        "return_fragment": False,
                        "streaming_mode": False,
                    }
                )
            )
        if len(output) != 1:
            raise RuntimeError("official inference returned an invalid fragment count")
        sample_rate, audio = output[0]
        if speed_factor != 1.0:
            assert self._speed_change is not None
            with _silence_official_output():
                audio = self._speed_change(audio, speed_factor, sample_rate)
        return _wav_bytes(sample_rate, audio)

    def _import_official(self) -> ModuleType:
        if self._module is not None:
            return self._module
        source_text = str(self._source_root)
        gpt_text = str(self._source_root / "GPT_SoVITS")
        for selected in (gpt_text, source_text):
            if selected not in sys.path:
                sys.path.insert(0, selected)
        os.chdir(self._source_root)
        ffmpeg_bin = _resolve(self._root, self._manifest.common.ffmpeg_bin.path)
        ffmpeg_executable = ffmpeg_bin / "ffmpeg.exe"
        if not ffmpeg_executable.is_file():
            raise RuntimeError("trusted ffmpeg executable is unavailable")
        os.environ["PATH"] = str(ffmpeg_bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ["bert_path"] = str(  # noqa: SIM112 - pinned upstream name
            _resolve(self._root, self._manifest.common.bert.path)
        )
        open_jtalk_root = _resolve(
            self._root,
            self._manifest.common.open_jtalk.path,
        )
        if not open_jtalk_root.is_dir():
            raise RuntimeError("trusted Open JTalk dictionary is unavailable")
        pyopenjtalk: Any = importlib.import_module("pyopenjtalk")
        pyopenjtalk.OPEN_JTALK_DICT_DIR = open_jtalk_root.as_posix().encode("utf-8")
        sv_module: Any = importlib.import_module("sv")
        sv_module.sv_path = str(
            _resolve(
                self._root,
                self._manifest.common.speaker_verification.path,
            )
        )
        module = importlib.import_module("TTS_infer_pack.TTS")
        self._module = module
        self._speed_change = module.speed_change
        return module

    def _loaded_pair_matches(self, slot: GatewayVoiceSlot) -> bool:
        if self._tts is None:
            return False
        configs = getattr(self._tts, "configs", None)
        actual_gpt = getattr(configs, "t2s_weights_path", None)
        actual_sovits = getattr(configs, "vits_weights_path", None)
        if not isinstance(actual_gpt, str) or not isinstance(actual_sovits, str):
            return False
        expected_gpt = str(_resolve(self._root, slot.gpt_weight.path))
        expected_sovits = str(_resolve(self._root, slot.sovits_weight.path))
        return os.path.normcase(os.path.abspath(actual_gpt)) == os.path.normcase(
            os.path.abspath(expected_gpt)
        ) and os.path.normcase(os.path.abspath(actual_sovits)) == os.path.normcase(
            os.path.abspath(expected_sovits)
        )


def _resolve(root: Path, relative: str) -> Path:
    selected = root.joinpath(*relative.replace("\\", "/").split("/")).absolute()
    selected.relative_to(root)
    return selected


def _wav_bytes(sample_rate: object, audio: object) -> bytes:
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or not 8_000 <= sample_rate <= 192_000
    ):
        raise RuntimeError("official inference returned an invalid sample rate")
    size = getattr(audio, "size", None)
    if (
        getattr(audio, "ndim", None) != 1
        or not isinstance(size, int)
        or size <= 0
        or str(getattr(audio, "dtype", "")) != "int16"
        or not callable(getattr(audio, "tobytes", None))
    ):
        raise RuntimeError("official inference returned invalid audio")
    samples = cast(_PCMArray, audio)
    frames = samples.tobytes()
    if not isinstance(frames, bytes) or len(frames) != samples.size * 2:
        raise RuntimeError("official inference returned invalid audio")
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(frames)
    return output.getvalue()


@contextmanager
def _silence_official_output() -> Iterator[None]:
    """Discard upstream output because it contains private paths and prompts."""

    with (
        open(os.devnull, "w", encoding="utf-8") as sink,
        redirect_stdout(sink),
        redirect_stderr(sink),
    ):
        yield
