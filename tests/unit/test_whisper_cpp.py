"""Subprocess, parsing, cancellation, and cleanup tests for whisper.cpp."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import wave
from pathlib import Path

import pytest
from desktop_client.inputs.stt_contracts import (
    STTError,
    STTErrorCode,
    TranscriptionRequest,
    TranscriptionResult,
)
from desktop_client.inputs.whisper_cpp import WhisperCppConfig, WhisperCppProvider

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
if mode == "sleep":
    audit.write_text(str(os.getpid()), encoding="utf-8")
    def ignore_term(*_args):
        audit.with_suffix(".term").write_text("term", encoding="utf-8")
    signal.signal(signal.SIGTERM, ignore_term)
    time.sleep(60)
    raise SystemExit(0)

audit.write_text(json.dumps(args), encoding="utf-8")
if mode == "fail":
    raise SystemExit(7)
output_base = Path(args[args.index("--output-file") + 1])
output_path = output_base.with_suffix(".json")
if mode == "nooutput":
    pass
elif mode == "malformed":
    output_path.write_text("not-json", encoding="utf-8")
elif mode == "empty":
    output_path.write_text(json.dumps({"transcription": []}), encoding="utf-8")
elif mode == "oversize":
    output_path.write_text("x" * 1000, encoding="utf-8")
elif mode == "toplist":
    output_path.write_text("[]", encoding="utf-8")
elif mode == "nosegments":
    output_path.write_text(json.dumps({"result": {}}), encoding="utf-8")
elif mode == "badsegment":
    output_path.write_text(json.dumps({"transcription": [{"text": 4}]}), encoding="utf-8")
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


def _provider(
    tmp_path: Path,
    mode: str,
    *,
    grace: float = 0.05,
    max_output_bytes: int = 2 * 1024 * 1024,
) -> tuple[WhisperCppProvider, Path, Path]:
    script = tmp_path / "fake whisper cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    model = tmp_path / "model.bin"
    model.write_bytes(b"fake-model")
    audit = tmp_path / "audit.json"
    scratch = tmp_path / "scratch"
    provider = WhisperCppProvider(
        WhisperCppConfig(
            executable=Path(sys.executable),
            executable_prefix_args=(str(script), mode, str(audit)),
            model_path=model,
            temporary_directory=scratch,
            terminate_grace_seconds=grace,
            max_output_bytes=max_output_bytes,
            threads=3,
        )
    )
    return provider, audit, scratch


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_success_uses_argument_vector_parses_json_and_cleans_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider, audit, scratch = _provider(tmp_path, "success")
        audio = tmp_path / "voice ; touch NEVER_CREATED.wav"
        _write_pcm_wav(audio)

        result = await provider.transcribe(
            TranscriptionRequest(audio_path=audio, language="auto", timeout_seconds=2)
        )
        await provider.close()

        assert result.text == "你好， 世界。"
        assert result.language == "zh"
        assert result.segment_count == 2
        arguments = json.loads(audit.read_text(encoding="utf-8"))
        assert arguments[arguments.index("--file") + 1] == str(audio)
        assert arguments[arguments.index("--language") + 1] == "auto"
        assert arguments[arguments.index("--threads") + 1] == "3"
        assert not (tmp_path / "NEVER_CREATED.wav").exists()
        assert list(scratch.iterdir()) == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("fail", STTErrorCode.process_failed),
        ("malformed", STTErrorCode.invalid_response),
        ("empty", STTErrorCode.empty_transcript),
        ("oversize", STTErrorCode.output_too_large),
        ("nooutput", STTErrorCode.invalid_response),
        ("toplist", STTErrorCode.invalid_response),
        ("nosegments", STTErrorCode.invalid_response),
        ("badsegment", STTErrorCode.invalid_response),
    ],
)
def test_failure_shapes_are_typed_and_temporary_output_is_removed(
    tmp_path: Path, mode: str, expected: STTErrorCode
) -> None:
    async def scenario() -> None:
        max_bytes = 10 if mode == "oversize" else 2 * 1024 * 1024
        provider, _audit, scratch = _provider(tmp_path, mode, max_output_bytes=max_bytes)
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        with pytest.raises(STTError) as caught:
            await provider.transcribe(TranscriptionRequest(audio_path=audio, timeout_seconds=2))
        assert caught.value.code is expected
        assert list(scratch.iterdir()) == []
        await provider.close()

    asyncio.run(scenario())


def test_configuration_and_contract_validation_rejects_invalid_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="language"):
        TranscriptionRequest(audio_path=tmp_path / "x.wav", language=" ")
    with pytest.raises(ValueError, match="timeout"):
        TranscriptionRequest(audio_path=tmp_path / "x.wav", timeout_seconds=0)
    with pytest.raises(ValueError, match="threads"):
        WhisperCppConfig(executable=tmp_path / "x", model_path=tmp_path / "m", threads=0)
    with pytest.raises(ValueError, match="grace"):
        WhisperCppConfig(
            executable=tmp_path / "x",
            model_path=tmp_path / "m",
            terminate_grace_seconds=0,
        )
    with pytest.raises(ValueError, match="大小"):
        WhisperCppConfig(
            executable=tmp_path / "x",
            model_path=tmp_path / "m",
            max_audio_bytes=1,
        )
    with pytest.raises(ValueError, match="不能为空"):
        TranscriptionResult(text=" ")
    with pytest.raises(ValueError, match="负数"):
        TranscriptionResult(text="ok", segment_count=-1)


def test_missing_runtime_components_start_failure_and_closed_provider_are_typed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        missing_executable = WhisperCppProvider(
            WhisperCppConfig(executable=tmp_path / "none", model_path=tmp_path / "none-model")
        )
        with pytest.raises(STTError) as caught:
            await missing_executable.transcribe(TranscriptionRequest(audio_path=audio))
        assert caught.value.code is STTErrorCode.executable_missing

        model_missing = WhisperCppProvider(
            WhisperCppConfig(executable=Path(sys.executable), model_path=tmp_path / "none-model")
        )
        with pytest.raises(STTError) as caught:
            await model_missing.transcribe(TranscriptionRequest(audio_path=audio))
        assert caught.value.code is STTErrorCode.model_missing

        not_executable = tmp_path / "not-executable"
        not_executable.write_text("plain text", encoding="utf-8")
        model = tmp_path / "model.bin"
        model.write_bytes(b"model")
        cannot_start = WhisperCppProvider(
            WhisperCppConfig(executable=not_executable, model_path=model)
        )
        with pytest.raises(STTError) as caught:
            await cannot_start.transcribe(TranscriptionRequest(audio_path=audio))
        assert caught.value.code is STTErrorCode.process_start_failed

        await model_missing.close()
        await model_missing.close()
        with pytest.raises(STTError) as caught:
            await model_missing.transcribe(TranscriptionRequest(audio_path=audio))
        assert caught.value.code is STTErrorCode.closed

    asyncio.run(scenario())


def test_corrupt_wav_is_rejected_and_language_is_optional(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider, _audit, _scratch = _provider(tmp_path, "success")
        corrupt = tmp_path / "corrupt.wav"
        corrupt.write_bytes(b"not a wave")
        with pytest.raises(STTError) as caught:
            await provider.transcribe(TranscriptionRequest(audio_path=corrupt))
        assert caught.value.code is STTErrorCode.invalid_audio

        result_path = tmp_path / "manual.json"
        result_path.write_text(
            json.dumps({"result": "unexpected", "transcription": [{"text": " ok "}]}),
            encoding="utf-8",
        )
        result = provider._read_result(result_path)
        assert result.text == "ok"
        assert result.language is None
        await provider.close()

    asyncio.run(scenario())


def test_close_terminates_an_active_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider, pid_file, scratch = _provider(tmp_path, "sleep", grace=0.02)
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        task = asyncio.create_task(
            provider.transcribe(TranscriptionRequest(audio_path=audio, timeout_seconds=30))
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.005)
        await provider.close()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], STTError)
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert not _process_exists(pid)
        assert list(scratch.iterdir()) == []

    asyncio.run(scenario())


def test_timeout_escalates_from_terminate_to_kill_and_reaps_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider, pid_file, scratch = _provider(tmp_path, "sleep", grace=0.02)
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        with pytest.raises(STTError) as caught:
            await provider.transcribe(TranscriptionRequest(audio_path=audio, timeout_seconds=0.05))
        assert caught.value.code is STTErrorCode.timeout
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert not _process_exists(pid)
        assert list(scratch.iterdir()) == []
        await provider.close()

    asyncio.run(scenario())


def test_task_cancellation_terminates_process_and_removes_json_directory(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider, pid_file, scratch = _provider(tmp_path, "sleep", grace=0.02)
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        task = asyncio.create_task(
            provider.transcribe(TranscriptionRequest(audio_path=audio, timeout_seconds=30))
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.005)
        assert pid_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert not _process_exists(pid)
        assert list(scratch.iterdir()) == []
        await provider.close()

    asyncio.run(scenario())


def test_repeated_cancellation_cannot_interrupt_process_reaping(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider, pid_file, scratch = _provider(tmp_path, "sleep", grace=0.2)
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        task = asyncio.create_task(
            provider.transcribe(TranscriptionRequest(audio_path=audio, timeout_seconds=30))
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.005)
        task.cancel()
        term_file = pid_file.with_suffix(".term")
        for _ in range(100):
            if term_file.exists():
                break
            await asyncio.sleep(0.005)
        assert term_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert not _process_exists(pid)
        assert provider._processes == set()
        assert list(scratch.iterdir()) == []
        await provider.close()

    asyncio.run(scenario())


def test_concurrent_close_waiters_join_the_same_process_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider, pid_file, scratch = _provider(tmp_path, "sleep", grace=0.2)
        audio = tmp_path / "input.wav"
        _write_pcm_wav(audio)
        transcription = asyncio.create_task(
            provider.transcribe(TranscriptionRequest(audio_path=audio, timeout_seconds=30))
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.005)
        first_close = asyncio.create_task(provider.close())
        term_file = pid_file.with_suffix(".term")
        for _ in range(100):
            if term_file.exists():
                break
            await asyncio.sleep(0.005)
        second_close = asyncio.create_task(provider.close())
        await asyncio.sleep(0)
        assert not first_close.done()
        assert not second_close.done()
        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close
        await second_close
        result = await asyncio.gather(transcription, return_exceptions=True)
        assert isinstance(result[0], STTError)
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert not _process_exists(pid)
        assert list(scratch.iterdir()) == []

    asyncio.run(scenario())


def test_rejects_missing_or_non_pcm_inputs_without_starting_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider, _audit, _scratch = _provider(tmp_path, "success")
        missing = tmp_path / "missing.wav"
        with pytest.raises(STTError) as caught:
            await provider.transcribe(TranscriptionRequest(audio_path=missing))
        assert caught.value.code is STTErrorCode.invalid_audio

        stereo = tmp_path / "stereo.wav"
        with wave.open(str(stereo), "wb") as recording:
            recording.setnchannels(2)
            recording.setsampwidth(2)
            recording.setframerate(44_100)
            recording.writeframes(b"\x00" * 8)
        with pytest.raises(STTError) as caught:
            await provider.transcribe(TranscriptionRequest(audio_path=stereo))
        assert caught.value.code is STTErrorCode.invalid_audio
        await provider.close()

    asyncio.run(scenario())
