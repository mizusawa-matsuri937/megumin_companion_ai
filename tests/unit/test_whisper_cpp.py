"""MediaWorker-private whisper.cpp preflight and process lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import pytest
from app.media import stt as stt_module
from app.media.stt import (
    TranscriptionResult,
    WhisperCppConfig,
    WhisperCppRunner,
    WhisperRuntimeError,
)

FAKE_CLI = """\
import json
import os
import signal
import sys
import time
from pathlib import Path

mode = sys.argv[1]
audit = Path(sys.argv[2])
args = sys.argv[3:]
if args == ["--version"]:
    audit.write_text("version", encoding="ascii")
    raise SystemExit(0 if mode != "bad_version" else 9)
if mode == "sleep":
    audit.write_text(str(os.getpid()), encoding="ascii")
    def ignore_term(*_args):
        audit.with_suffix(".term").write_text("term", encoding="ascii")
    signal.signal(signal.SIGTERM, ignore_term)
    time.sleep(60)
    raise SystemExit(0)
audit.write_text(json.dumps(args), encoding="utf-8")
output_base = Path(args[args.index("--output-file") + 1])
output_path = output_base.with_suffix(".json")
if mode == "fail":
    raise SystemExit(7)
if mode == "malformed":
    output_path.write_text("not-json", encoding="utf-8")
elif mode == "empty":
    output_path.write_text(json.dumps({"transcription": []}), encoding="utf-8")
elif mode == "oversize":
    output_path.write_text("x" * 1000, encoding="utf-8")
else:
    output_path.write_text(json.dumps({
        "result": {"language": "zh"},
        "transcription": [{"text": " 你好，"}, {"text": " 世界。 "}],
    }), encoding="utf-8")
"""


def _write_pcm_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16_000)
        recording.writeframes(b"\x00\x00" * 160)


def _runner(
    tmp_path: Path,
    mode: str,
    *,
    grace: float = 0.05,
    max_output_bytes: int = 2 * 1024 * 1024,
) -> tuple[WhisperCppRunner, Path]:
    script = tmp_path / "假 whisper cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    model = tmp_path / "模型 文件.bin"
    model.write_bytes(b"fake-model")
    audit = tmp_path / "audit.txt"
    return (
        WhisperCppRunner(
            WhisperCppConfig(
                executable=Path(sys.executable),
                executable_prefix_args=(str(script), mode, str(audit)),
                model_path=model,
                terminate_grace_seconds=grace,
                max_output_bytes=max_output_bytes,
                threads=3,
            )
        ),
        audit,
    )


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
        )
        return f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class _ControlledProcess:
    def __init__(self, *, settle_on_terminate: bool = True) -> None:
        self.returncode: int | None = None
        self.settle_on_terminate = settle_on_terminate
        self.terminated = 0
        self.killed = 0
        self._settled = asyncio.Event()

    async def wait(self) -> int:
        await self._settled.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        if self.settle_on_terminate:
            self.returncode = -15
            self._settled.set()

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9
        self._settled.set()


def test_preflight_probes_version_architecture_and_fingerprints_then_transcribes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runner, audit = _runner(tmp_path, "success")
        audio = tmp_path / "中文 空格.wav"
        _write_pcm_wav(audio)

        result = await runner.transcribe(
            audio,
            language="auto",
            timeout_seconds=2,
            cancelled=asyncio.Event(),
        )

        assert result.text == "你好， 世界。"
        assert result.language == "zh"
        assert result.segment_count == 2
        assert audit.read_text(encoding="utf-8") != "version"
        assert runner._fingerprint is not None
        assert len(runner._fingerprint.executable_sha256) == 64
        assert len(runner._fingerprint.model_fingerprint) == 64
        await runner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("fail", "stt_process_failed"),
        ("malformed", "stt_invalid_response"),
        ("empty", "stt_empty_transcript"),
        ("oversize", "stt_output_too_large"),
    ],
)
def test_typed_failures_do_not_expose_cli_output(tmp_path: Path, mode: str, expected: str) -> None:
    async def scenario() -> None:
        runner, _audit = _runner(
            tmp_path,
            mode,
            max_output_bytes=10 if mode == "oversize" else 2 * 1024 * 1024,
        )
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        with pytest.raises(WhisperRuntimeError) as caught:
            await runner.transcribe(
                audio,
                language="auto",
                timeout_seconds=2,
                cancelled=asyncio.Event(),
            )
        assert caught.value.code == expected
        assert "whisper" not in str(caught.value).casefold() or str(caught.value) == expected
        await runner.close()

    asyncio.run(scenario())


def test_preflight_fails_closed_for_missing_runtime_or_bad_version(tmp_path: Path) -> None:
    async def scenario() -> None:
        missing = WhisperCppRunner(
            WhisperCppConfig(executable=tmp_path / "missing.exe", model_path=tmp_path / "model.bin")
        )
        with pytest.raises(WhisperRuntimeError) as caught:
            await missing.preflight()
        assert caught.value.code == "stt_executable_missing"

        runner, _audit = _runner(tmp_path, "bad_version")
        with pytest.raises(WhisperRuntimeError) as caught:
            await runner.preflight()
        assert caught.value.code == "stt_version_probe_failed"
        await runner.close()

    asyncio.run(scenario())


def test_timeout_and_cancellation_reap_the_direct_whisper_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner, audit = _runner(tmp_path, "sleep", grace=0.02)
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        with pytest.raises(WhisperRuntimeError) as caught:
            await runner.transcribe(
                audio,
                language="auto",
                timeout_seconds=0.25,
                cancelled=asyncio.Event(),
            )
        assert caught.value.code == "stt_timeout"
        pid = int(audit.read_text(encoding="ascii"))
        assert not _process_exists(pid)

        cancellation = asyncio.Event()
        task = asyncio.create_task(
            runner.transcribe(
                audio,
                language="auto",
                timeout_seconds=30,
                cancelled=cancellation,
            )
        )
        for _ in range(100):
            if audit.exists() and audit.read_text(encoding="ascii").isdigit():
                break
            await asyncio.sleep(0.005)
        cancellation.set()
        with pytest.raises(WhisperRuntimeError) as caught:
            await task
        assert caught.value.code == "stt_cancelled"
        assert not _process_exists(int(audit.read_text(encoding="ascii")))
        await runner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "options",
    [
        {"threads": 0},
        {"terminate_grace_seconds": 0},
        {"max_audio_bytes": 43},
        {"max_output_bytes": 0},
        {"executable_prefix_args": ("",)},
        {"executable_prefix_args": ("bad\x00arg",)},
    ],
)
def test_whisper_config_rejects_invalid_private_process_limits(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WhisperCppConfig(Path("cli"), Path("model"), **options)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "  "},
        {"text": "bad\x00text"},
        {"text": "x" * 4_097},
        {"text": "ok", "language": ""},
        {"text": "ok", "language": "bad\x00language"},
        {"text": "ok", "language": "x" * 65},
        {"text": "ok", "segment_count": -1},
    ],
)
def test_transcription_result_enforces_helper_boundary(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        TranscriptionResult(**kwargs)  # type: ignore[arg-type]

    assert TranscriptionResult("  合成文本  ", "zh", 0).text == "合成文本"


def test_runner_rejects_invalid_audio_and_untrusted_json_shapes(tmp_path: Path) -> None:
    runner, _audit = _runner(tmp_path, "success")
    bad_audio = tmp_path / "stereo.wav"
    with wave.open(str(bad_audio), "wb") as recording:
        recording.setnchannels(2)
        recording.setsampwidth(2)
        recording.setframerate(16_000)
        recording.writeframes(b"\x00\x00" * 32)
    with pytest.raises(WhisperRuntimeError, match="stt_invalid_audio"):
        runner._validate_audio(bad_audio)

    response = tmp_path / "transcript.json"
    for payload, expected in (
        ([], "stt_invalid_response"),
        ({"transcription": "not-a-list"}, "stt_invalid_response"),
        ({"transcription": [{"missing": "text"}]}, "stt_invalid_response"),
        ({"transcription": [{"text": "x" * 4_097}]}, "stt_transcript_too_large"),
        (
            {"result": {"language": "x" * 65}, "transcription": [{"text": "ok"}]},
            "stt_invalid_response",
        ),
    ):
        response.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(WhisperRuntimeError) as caught:
            runner._read_result(response)
        assert caught.value.code == expected


def test_preflight_rejects_architecture_mismatch_and_caches_a_healthy_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        runner, _audit = _runner(tmp_path, "success")
        with monkeypatch.context() as patched:
            patched.setattr(stt_module, "_executable_architecture", lambda _path: "arm64")
            patched.setattr(stt_module, "_host_architecture", lambda: "x64")
            with pytest.raises(WhisperRuntimeError) as caught:
                await runner.preflight()
            assert caught.value.code == "stt_architecture_incompatible"
        await runner.close()

        healthy_dir = tmp_path / "healthy"
        healthy_dir.mkdir()
        # A second preflight uses the cached signature instead of spawning
        # another probe.
        healthy, healthy_audit = _runner(healthy_dir, "success")
        await healthy.preflight()
        assert healthy_audit.read_text(encoding="ascii") == "version"
        healthy_audit.unlink()
        await healthy.preflight()
        assert not healthy_audit.exists()
        await healthy.close()

    asyncio.run(scenario())


def test_version_timeout_start_failure_and_kill_fallback_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        runner, _audit = _runner(tmp_path, "success", grace=0.001)
        timeout_process = _ControlledProcess()

        async def start_timeout(_command: object) -> _ControlledProcess:
            return timeout_process

        monkeypatch.setattr(runner, "_start_process", start_timeout)
        monkeypatch.setattr(stt_module, "_VERSION_TIMEOUT_SECONDS", 0.001)
        with pytest.raises(WhisperRuntimeError) as caught:
            await runner._probe_version(Path("ignored"))
        assert caught.value.code == "stt_version_probe_timeout"
        assert timeout_process.terminated == 1

        start_dir = tmp_path / "start"
        start_dir.mkdir()
        failing_runner, _failing_audit = _runner(start_dir, "success")

        async def start_failure(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("synthetic")

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            start_failure,
        )
        with pytest.raises(WhisperRuntimeError) as caught:
            await failing_runner._start_process(("missing",))
        assert caught.value.code == "stt_process_start_failed"

        kill_process = _ControlledProcess(settle_on_terminate=False)
        await runner._terminate(kill_process)  # type: ignore[arg-type]
        assert kill_process.terminated == 1 and kill_process.killed == 1

    asyncio.run(scenario())


def test_runner_rejects_closed_timeout_invalid_audio_and_unbounded_runtime_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        runner, _audit = _runner(tmp_path, "success")
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        with pytest.raises(WhisperRuntimeError) as caught:
            await runner.transcribe(
                audio,
                language="zh",
                timeout_seconds=0,
                cancelled=asyncio.Event(),
            )
        assert caught.value.code == "stt_timeout_invalid"

        await runner.close()

        async def skip_preflight() -> None:
            return None

        monkeypatch.setattr(runner, "preflight", skip_preflight)
        with pytest.raises(WhisperRuntimeError) as caught:
            await runner.transcribe(
                audio,
                language="zh",
                timeout_seconds=1,
                cancelled=asyncio.Event(),
            )
        assert caught.value.code == "stt_worker_closed"

        bounded = WhisperCppRunner(
            WhisperCppConfig(
                executable=Path(sys.executable),
                model_path=tmp_path / "model.bin",
                max_audio_bytes=44,
            )
        )
        (tmp_path / "model.bin").write_bytes(b"model")
        with pytest.raises(WhisperRuntimeError) as caught:
            bounded._validate_audio(audio)
        assert caught.value.code == "stt_invalid_audio"

        malformed = tmp_path / "malformed.wav"
        malformed.write_bytes(b"not a wav")
        with pytest.raises(WhisperRuntimeError) as caught:
            bounded._validate_audio(malformed)
        assert caught.value.code == "stt_invalid_audio"

        response = tmp_path / "language-omitted.json"
        response.write_text(json.dumps({"transcription": [{"text": "ok"}]}), encoding="utf-8")
        assert bounded._read_result(response).language is None
        with pytest.raises(WhisperRuntimeError) as caught:
            stt_module._validated_regular_file(tmp_path, "stt_invalid_response")
        assert caught.value.code == "stt_invalid_response"

        large_model = tmp_path / "large-model.bin"
        large_model.write_bytes(b"a" * (2 * 64 * 1024))
        assert len(stt_module._sampled_fingerprint(large_model)) == 64
        with monkeypatch.context() as patched:
            patched.setattr(platform, "machine", lambda: "aarch64")
            assert stt_module._host_architecture() == "arm64"
            patched.setattr(platform, "machine", lambda: "mystery")
            assert stt_module._host_architecture() == "mystery"
            patched.setattr(struct, "calcsize", lambda _format: 4)
            with pytest.raises(WhisperRuntimeError) as caught:
                bounded._inspect_runtime()
            assert caught.value.code == "stt_architecture_incompatible"

    asyncio.run(scenario())
