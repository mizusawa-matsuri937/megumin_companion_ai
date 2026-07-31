from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from app.tts_gateway.contracts import VoiceSlot
from app.tts_gateway.manifest import (
    FileDigest,
    GatewayCommonAssets,
    GatewayManifest,
    GatewayVoiceSlot,
    TreeDigest,
)
from app.tts_gateway.official_backend import OfficialGPTSoVITSBackend


class _PCM:
    ndim = 1
    dtype = "int16"

    def __init__(self, frames: bytes = b"\x00\x00" * 320) -> None:
        self._frames = frames
        self.size = len(frames) // 2

    def tobytes(self) -> bytes:
        return self._frames


def _slot(name: str) -> GatewayVoiceSlot:
    return GatewayVoiceSlot(
        gpt_weight=FileDigest(path=f"slots/{name}/gpt.ckpt", sha256="a" * 64),
        sovits_weight=FileDigest(path=f"slots/{name}/sovits.pth", sha256="b" * 64),
        reference_audio=FileDigest(path=f"slots/{name}/reference.wav", sha256="c" * 64),
        prompt_text="これは日本語の参照文です",
        prompt_lang="ja",
        text_lang="zh",
        archive_sha256="d" * 64,
    )


def _manifest() -> GatewayManifest:
    slots: dict[VoiceSlot, GatewayVoiceSlot] = {
        "neutral": _slot("neutral"),
        "gentle": _slot("gentle"),
        "tsundere": _slot("tsundere"),
        "focused": _slot("focused"),
        "excited_explosion": _slot("excited_explosion"),
    }
    return GatewayManifest(
        format_version=1,
        source_commit="d523079fc05d9a8028d6085bffe4a2757c32abb6",
        source_root="source",
        source_tree_sha256="e" * 64,
        common=GatewayCommonAssets(
            bert=TreeDigest(path="common/bert", sha256="f" * 64),
            cnhubert=TreeDigest(path="common/hubert", sha256="0" * 64),
            ffmpeg_bin=TreeDigest(path="common/ffmpeg", sha256="3" * 64),
            g2pw=TreeDigest(
                path="source/GPT_SoVITS/text/G2PWModel",
                sha256="2" * 64,
            ),
            language_detection=TreeDigest(
                path="source/GPT_SoVITS/pretrained_models/fast_langdetect",
                sha256="4" * 64,
            ),
            open_jtalk=TreeDigest(
                path="common/open-jtalk",
                sha256="5" * 64,
            ),
            speaker_verification=FileDigest(
                path="common/sv.ckpt",
                sha256="1" * 64,
            ),
        ),
        batch_size=10,
        slots=slots,
        device="cuda",
        is_half=True,
    )


def test_official_backend_uses_hashed_sv_fixed_parameters_and_post_speed(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    manifest = _manifest()
    root = tmp_path / "private"
    (root / "source" / "GPT_SoVITS").mkdir(parents=True)
    ffmpeg = root / "common" / "ffmpeg" / "ffmpeg.exe"
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.write_bytes(b"ffmpeg")
    open_jtalk = root / "common" / "open-jtalk"
    open_jtalk.mkdir()
    sv_module = ModuleType("sv")
    pyopenjtalk_module = ModuleType("pyopenjtalk")
    tts_module = ModuleType("TTS_infer_pack.TTS")
    observed: dict[str, Any] = {"loads": []}

    class Config:
        def __init__(self, value: dict[str, object]) -> None:
            observed["config"] = value
            custom = value["custom"]
            assert isinstance(custom, dict)
            self.version = custom["version"]
            self.t2s_weights_path = custom["t2s_weights_path"]
            self.vits_weights_path = custom["vits_weights_path"]

    class TTS:
        def __init__(self, config: Config) -> None:
            print("private-model-path-must-not-leak")
            self.configs = SimpleNamespace(
                version=config.version,
                t2s_weights_path=config.t2s_weights_path,
                vits_weights_path=config.vits_weights_path,
            )

        def init_t2s_weights(self, path: str) -> None:
            print(path)
            observed["loads"].append(("gpt", path))
            self.configs.t2s_weights_path = path

        def init_vits_weights(self, path: str) -> None:
            print(path)
            observed["loads"].append(("sovits", path))
            self.configs.vits_weights_path = path

        def run(self, values: dict[str, object]) -> list[tuple[int, _PCM]]:
            print(values["prompt_text"])
            observed["run"] = values
            return [(32_000, _PCM())]

    def speed_change(audio: _PCM, speed: float, sample_rate: int) -> _PCM:
        observed["speed"] = (speed, sample_rate, audio.size)
        return audio

    dynamic_module: Any = tts_module
    dynamic_module.TTS_Config = Config
    dynamic_module.TTS = TTS
    dynamic_module.speed_change = speed_change

    def import_module(name: str) -> ModuleType:
        if name == "pyopenjtalk":
            return pyopenjtalk_module
        if name == "sv":
            return sv_module
        if name == "TTS_infer_pack.TTS":
            return tts_module
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", import_module)
    monkeypatch.delenv("bert_path", raising=False)
    backend = OfficialGPTSoVITSBackend(manifest, root)
    backend.load_initial(manifest.slots["neutral"])
    backend.load_pair(manifest.slots["gentle"])
    wav = backend.synthesize(
        manifest.slots["gentle"],
        "这是固定中文测试句。",
        speed_factor=1.05,
        batch_size=10,
    )

    assert wav[:4] == b"RIFF"
    assert os.environ["bert_path"] == str(  # noqa: SIM112 - pinned upstream name
        root / "common" / "bert"
    )
    assert os.environ["PATH"].split(os.pathsep, 1)[0] == str(ffmpeg.parent)
    assert open_jtalk.as_posix().encode("utf-8") == pyopenjtalk_module.OPEN_JTALK_DICT_DIR
    assert sv_module.sv_path == str(root / "common" / "sv.ckpt")
    config = observed["config"]["custom"]
    assert config["version"] == "v2ProPlus"
    assert config["device"] == "cuda"
    assert config["is_half"] is True
    run = observed["run"]
    assert run == {
        "text": "这是固定中文测试句。",
        "text_lang": "zh",
        "ref_audio_path": str(root / "slots" / "gentle" / "reference.wav"),
        "aux_ref_audio_paths": [],
        "prompt_text": "これは日本語の参照文です",
        "prompt_lang": "ja",
        "top_k": 5,
        "top_p": 1.0,
        "temperature": 1.0,
        "text_split_method": "cut1",
        "batch_size": 10,
        "batch_threshold": 0.75,
        "split_bucket": True,
        "speed_factor": 1.0,
        "fragment_interval": 0.3,
        "seed": -1,
        "parallel_infer": True,
        "repetition_penalty": 1.35,
        "return_fragment": False,
        "streaming_mode": False,
    }
    assert observed["speed"] == (1.05, 32_000, 320)
    assert [kind for kind, _path in observed["loads"]] == ["gpt", "sovits"]
    assert capsys.readouterr().out == ""


def test_official_backend_rejects_a_partially_updated_weight_pair(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    backend = OfficialGPTSoVITSBackend(manifest, tmp_path)

    class PartialTTS:
        def __init__(self) -> None:
            self.configs = SimpleNamespace(
                version="v2ProPlus",
                t2s_weights_path=str(tmp_path / "old-gpt.ckpt"),
                vits_weights_path=str(tmp_path / "old-sovits.pth"),
            )

        def init_t2s_weights(self, path: str) -> None:
            self.configs.t2s_weights_path = path

        def init_vits_weights(self, _path: str) -> None:
            return

    backend._tts = PartialTTS()

    with pytest.raises(RuntimeError, match="load verification failed"):
        backend.load_pair(manifest.slots["gentle"])
