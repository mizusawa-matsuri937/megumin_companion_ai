"""Regression tests for the explicit, privacy-bounded STT smoke tool."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from app.media.voice import VoiceCaptureError


def _load_smoke_tool() -> ModuleType:
    path = Path(__file__).parents[2] / "tools" / "stt_smoke.py"
    spec = importlib.util.spec_from_file_location("w18_stt_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _EmptyRecordingRecorder:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> object:
        raise VoiceCaptureError("stt_empty_recording")

    async def close(self) -> None:
        self.closed = True


def test_microphone_empty_recording_is_stable_and_path_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _load_smoke_tool()
    recorder = _EmptyRecordingRecorder()

    monkeypatch.setattr(
        tool,
        "_arguments",
        lambda: argparse.Namespace(
            config=None,
            env_file=None,
            mode="microphone",
            measure_working_set=True,
        ),
    )
    monkeypatch.setattr(
        tool,
        "load_settings",
        lambda *_args: SimpleNamespace(stt=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr(tool, "create_media_worker_voice_input", lambda _settings: recorder)

    async def skip_wait(_prompt: str) -> None:
        return None

    monkeypatch.setattr(tool, "_wait_for_enter", skip_wait)

    assert asyncio.run(tool._run()) == 2
    output = capsys.readouterr().out.splitlines()
    assert "录音已开始" in output[0]
    assert json.loads(output[1]) == {
        "reason_code": "stt_empty_recording",
        "status": "error",
    }
    assert recorder.started
    assert recorder.closed
