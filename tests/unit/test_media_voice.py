"""Synthetic W18 PTT tests: raw microphone data never leaves MediaWorker."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import wave
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from app.media import voice as voice_module
from app.media import worker as media_worker
from app.media.stt import TranscriptionResult, WhisperCppConfig, WhisperRuntimeError
from app.media.voice import (
    MediaWorkerVoiceInput,
    VoiceCaptureError,
    VoiceCaptureState,
    VoiceTranscription,
)
from app.paths import AppPaths
from app.temp_assets import TempAssetRegistry
from app.windows_security import PortableDirectorySecurity
from app.workers import (
    ApprovedResourcePolicy,
    SupervisorConfig,
    WorkerSupervisor,
    process_adapter_for_current_platform,
)
from app.workers.access import AuthorizedResource


class _FakeInputStream:
    def __init__(self, callback: media_worker.PCMFrameCallback) -> None:
        self._callback = callback
        self.active = False
        self.start_count = 0
        self.stop_count = 0
        self.abort_count = 0
        self.close_count = 0

    def start(self) -> None:
        self.active = True
        self.start_count += 1

    def stop(self) -> None:
        self.active = False
        self.stop_count += 1

    def abort(self) -> None:
        self.active = False
        self.abort_count += 1

    def close(self) -> None:
        self.active = False
        self.close_count += 1

    def emit(self, payload: bytes, *, status_present: bool = False) -> bool:
        return self._callback(memoryview(payload), status_present)


class _VoiceBackend:
    def __init__(self) -> None:
        self.input_streams: list[_FakeInputStream] = []
        self.input_options: list[dict[str, object]] = []

    def output_devices(self) -> tuple[media_worker._DiscoveredOutput, ...]:
        return ()

    def open_output_stream(
        self,
        _audio_format: media_worker._WaveFormat,
        *,
        native_index: int,
        latency: str,
    ) -> media_worker._OutputStream:
        del native_index, latency
        raise AssertionError("PTT test must not open an output stream")

    def open_input_stream(
        self,
        *,
        sample_rate: int,
        channels: int,
        device: int | str | None,
        blocksize: int,
        callback: media_worker.PCMFrameCallback,
    ) -> _FakeInputStream:
        self.input_options.append(
            {
                "sample_rate": sample_rate,
                "channels": channels,
                "device": device,
                "blocksize": blocksize,
            }
        )
        stream = _FakeInputStream(callback)
        self.input_streams.append(stream)
        return stream


class _FakeWhisperRunner:
    instances: list[_FakeWhisperRunner] = []
    failure_code: str | None = None
    preflight_failure_code: str | None = None

    def __init__(self, _config: WhisperCppConfig) -> None:
        self.preflight_count = 0
        self.audio_paths: list[Path] = []
        self.closed = False
        self.instances.append(self)

    async def preflight(self) -> None:
        self.preflight_count += 1
        if self.preflight_failure_code is not None:
            raise WhisperRuntimeError(self.preflight_failure_code)

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str,
        timeout_seconds: float,
        cancelled: asyncio.Event,
    ) -> TranscriptionResult:
        del timeout_seconds
        assert not cancelled.is_set()
        self.audio_paths.append(audio_path)
        with wave.open(str(audio_path), "rb") as recorded:
            assert (
                recorded.getnchannels(),
                recorded.getsampwidth(),
                recorded.getframerate(),
            ) == (1, 2, 16_000)
            assert recorded.readframes(recorded.getnframes())
        if self.failure_code is not None:
            raise WhisperRuntimeError(self.failure_code)
        return TranscriptionResult("合成转写", language if language != "auto" else "zh", 1)

    async def close(self) -> None:
        self.closed = True


def _handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    maximum_recording_seconds: float = 1.0,
    recording_watchdog_wait: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[media_worker.MediaWorkerHandler, _VoiceBackend, Path]:
    _FakeWhisperRunner.instances.clear()
    _FakeWhisperRunner.failure_code = None
    _FakeWhisperRunner.preflight_failure_code = None
    monkeypatch.setattr(media_worker, "WhisperCppRunner", _FakeWhisperRunner)
    root = tmp_path / "私有 STT 根"
    root.mkdir()
    backend = _VoiceBackend()
    handler = media_worker.MediaWorkerHandler(
        backend=backend,
        stt_config=WhisperCppConfig(
            executable=tmp_path / "whisper-cli.exe",
            model_path=tmp_path / "model.bin",
        ),
        recording_root=root,
        input_device="合成麦克风",
        input_blocksize=160,
        maximum_recording_seconds=maximum_recording_seconds,
        transcription_timeout_seconds=2.0,
        language="zh",
        recording_watchdog_wait=recording_watchdog_wait,
    )
    return handler, backend, root


def test_media_worker_ptt_opens_input_only_after_start_and_cleans_private_wav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        handler, backend, root = _handler(tmp_path, monkeypatch)
        assert backend.input_streams == []
        assert await handler.run_job(
            "media.stt_preflight", (), asyncio.Event(), job_id="preflight"
        ) == {"status": "ready"}
        assert backend.input_streams == []

        started = await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="capture-start"
        )
        assert started == {"status": "recording"}
        stream = backend.input_streams[-1]
        assert stream.active and stream.start_count == 1
        assert stream.emit(b"\x01\x00" * 320)

        result = await handler.run_job(
            "media.record_stop", (), asyncio.Event(), job_id="capture-stop"
        )
        assert result == {
            "status": "transcribed",
            "text": "合成转写",
            "language": "zh",
            "segment_count": 1,
        }
        assert stream.stop_count == 1 and stream.close_count == 1
        assert _FakeWhisperRunner.instances[-1].audio_paths
        assert list(root.iterdir()) == []
        await handler.close()

    asyncio.run(scenario())


def test_media_worker_ptt_removes_private_recording_after_transcription_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        handler, backend, root = _handler(tmp_path, monkeypatch)
        _FakeWhisperRunner.failure_code = "stt_process_failed"
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="failure-start"
        ) == {"status": "recording"}
        assert backend.input_streams[-1].emit(b"\x01\x00" * 320)

        result = await handler.run_job(
            "media.record_stop", (), asyncio.Event(), job_id="failure-stop"
        )
        assert result == {"status": "failed", "error_code": "stt_process_failed"}
        assert list(root.iterdir()) == []
        await handler.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "status_present", "expected"),
    [
        (b"\x00" * 322, False, "stt_capture_overflow"),
        (b"\x00\x00", True, "stt_capture_device_lost"),
    ],
)
def test_media_worker_ptt_surfaces_callback_failure_without_queuing_pcm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    status_present: bool,
    expected: str,
) -> None:
    async def scenario() -> None:
        handler, backend, root = _handler(
            tmp_path,
            monkeypatch,
            maximum_recording_seconds=0.01,
        )
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="overflow"
        ) == {"status": "recording"}
        assert not backend.input_streams[-1].emit(payload, status_present=status_present)
        result = await handler.run_job("media.record_stop", (), asyncio.Event(), job_id="stop")
        assert result == {"status": "failed", "error_code": expected}
        assert list(root.iterdir()) == []
        await handler.close()

    asyncio.run(scenario())
    source = (Path(media_worker.__file__)).read_text(encoding="utf-8")
    assert "call_soon_threadsafe" not in source
    assert "asyncio.Queue" not in source


def test_media_worker_ptt_enforces_the_120_second_design_cap_with_worker_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        watchdog_started = asyncio.Event()
        watchdog_elapsed = asyncio.Event()

        async def wait_for_limit(_seconds: float) -> None:
            watchdog_started.set()
            await watchdog_elapsed.wait()

        handler, backend, root = _handler(
            tmp_path,
            monkeypatch,
            maximum_recording_seconds=0.01,
            recording_watchdog_wait=wait_for_limit,
        )
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="timeout"
        ) == {"status": "recording"}
        await asyncio.wait_for(watchdog_started.wait(), timeout=1)
        session = handler._recording
        assert session is not None
        watchdog_elapsed.set()
        await asyncio.wait_for(session.watchdog, timeout=1)
        result = await handler.run_job(
            "media.record_stop", (), asyncio.Event(), job_id="timeout-stop"
        )
        assert result == {"status": "failed", "error_code": "stt_recording_too_long"}
        stream = backend.input_streams[-1]
        assert stream.abort_count + stream.stop_count >= 1 and stream.close_count >= 1
        assert list(root.iterdir()) == []
        await handler.close()

    asyncio.run(scenario())


def test_pcm_ring_is_bounded_wiped_and_reports_callback_failures() -> None:
    with pytest.raises(ValueError):
        media_worker._PCMRecordingRing(1)

    recording = media_worker._PCMRecordingRing(8)
    assert recording.append_from_callback(memoryview(b"\x01\x00\x02\x00"), False)
    pcm, failure = recording.drain()
    assert pcm == b"\x01\x00\x02\x00" and failure is None

    rejected = media_worker._PCMRecordingRing(8)
    assert rejected.append_from_callback(memoryview(b"\x00"), False) is False
    _pcm, failure = rejected.drain()
    assert failure == "stt_capture_device_lost"

    overflow = media_worker._PCMRecordingRing(4)
    assert overflow.append_from_callback(memoryview(b"\x01\x00\x02\x00"), False)
    assert overflow.append_from_callback(memoryview(b"\x03\x00"), False) is False
    _pcm, failure = overflow.drain()
    assert failure == "stt_capture_overflow"

    non_contiguous = memoryview(b"\x01\x00\x02\x00")[::2]
    assert media_worker._PCMRecordingRing(8).append_from_callback(non_contiguous, False) is False
    failed = media_worker._PCMRecordingRing(4)
    failed.fail("first_failure")
    failed.fail("later_failure")
    _pcm, failure = failed.drain()
    assert failure == "first_failure"


def test_ptt_worker_protocol_disabled_and_capture_terminal_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        disabled = media_worker.MediaWorkerHandler(backend=_VoiceBackend())
        cancelled = asyncio.Event()
        unapproved = (AuthorizedResource("unapproved", 0),)
        assert await disabled.run_job("media.record_start", (), cancelled, job_id="disabled") == {
            "status": "failed",
            "error_code": "stt_disabled",
        }
        assert await disabled.run_job("media.stt_preflight", (), cancelled, job_id="disabled") == {
            "status": "failed",
            "error_code": "stt_disabled",
        }
        assert await disabled.run_job(
            "media.stt_preflight", unapproved, asyncio.Event(), job_id="bad"
        ) == {"status": "failed", "error_code": "stt_protocol_invalid"}
        assert await disabled.run_job(
            "media.record_start", unapproved, asyncio.Event(), job_id="bad"
        ) == {"status": "failed", "error_code": "stt_protocol_invalid"}
        assert await disabled.run_job(
            "media.record_stop", unapproved, asyncio.Event(), job_id="bad"
        ) == {"status": "failed", "error_code": "stt_protocol_invalid"}
        assert await disabled.run_job(
            "media.record_cancel", unapproved, asyncio.Event(), job_id="bad"
        ) == {"status": "failed", "error_code": "stt_protocol_invalid"}
        assert await disabled.run_job("media.record_stop", (), asyncio.Event(), job_id="none") == {
            "status": "failed",
            "error_code": "stt_recording_not_active",
        }
        await disabled.close()

        handler, backend, root = _handler(tmp_path, monkeypatch)
        assert await handler.run_job("media.record_start", (), asyncio.Event(), job_id="empty") == {
            "status": "recording"
        }
        assert await handler.run_job(
            "media.record_stop", (), asyncio.Event(), job_id="empty-stop"
        ) == {"status": "failed", "error_code": "stt_empty_recording"}
        assert list(root.iterdir()) == []

        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="cancelled"
        ) == {"status": "recording"}
        assert backend.input_streams[-1].emit(b"\x01\x00" * 20)
        cancelled.set()
        assert await handler.run_job(
            "media.record_stop", (), cancelled, job_id="cancelled-stop"
        ) == {"status": "cancelled"}
        assert list(root.iterdir()) == []
        await handler.close()

    asyncio.run(scenario())


def test_ptt_worker_handles_cancelled_start_preflight_failure_active_and_cancel_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        handler, _backend, root = _handler(tmp_path, monkeypatch)
        cancelled = asyncio.Event()
        cancelled.set()
        assert await handler.run_job(
            "media.record_start", (), cancelled, job_id="already-cancelled"
        ) == {"status": "cancelled"}

        _FakeWhisperRunner.preflight_failure_code = "stt_version_probe_failed"
        assert await handler.run_job(
            "media.stt_preflight", (), asyncio.Event(), job_id="failed-preflight"
        ) == {"status": "failed", "error_code": "stt_version_probe_failed"}
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="failed-start"
        ) == {"status": "failed", "error_code": "stt_version_probe_failed"}

        _FakeWhisperRunner.preflight_failure_code = None
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="active"
        ) == {"status": "recording"}
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="another"
        ) == {"status": "failed", "error_code": "stt_recording_active"}
        assert await handler.run_job(
            "media.record_cancel", (), asyncio.Event(), job_id="cancel-active"
        ) == {"status": "cancelled"}
        assert list(root.iterdir()) == []
        await handler.close()

    asyncio.run(scenario())


def test_ptt_worker_transcription_cancel_and_internal_stop_error_are_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        handler, backend, root = _handler(tmp_path, monkeypatch)
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="whisper-cancel"
        ) == {"status": "recording"}
        assert backend.input_streams[-1].emit(b"\x01\x00" * 20)
        _FakeWhisperRunner.failure_code = "stt_cancelled"
        assert await handler.run_job(
            "media.record_stop", (), asyncio.Event(), job_id="whisper-cancel-stop"
        ) == {"status": "cancelled"}
        assert list(root.iterdir()) == []

        _FakeWhisperRunner.failure_code = None
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="write-failure"
        ) == {"status": "recording"}
        assert backend.input_streams[-1].emit(b"\x01\x00" * 20)

        def fail_write(_path: Path, _pcm: bytearray) -> None:
            raise OSError("synthetic wav write failure")

        monkeypatch.setattr(media_worker, "_write_voice_wav", fail_write)
        assert await handler.run_job(
            "media.record_stop", (), asyncio.Event(), job_id="write-failure-stop"
        ) == {"status": "failed", "error_code": "stt_capture_stop_failed"}
        assert list(root.iterdir()) == []
        await handler.close()

    asyncio.run(scenario())


def test_ptt_capture_start_failure_closes_stream_and_removes_private_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        handler, backend, root = _handler(tmp_path, monkeypatch)

        def unavailable(**_kwargs: object) -> _FakeInputStream:
            raise OSError("synthetic input failure")

        monkeypatch.setattr(backend, "open_input_stream", unavailable)
        assert await handler.run_job(
            "media.record_start", (), asyncio.Event(), job_id="input-failure"
        ) == {"status": "failed", "error_code": "stt_capture_start_failed"}
        assert list(root.iterdir()) == []
        await handler.close()

    asyncio.run(scenario())


def test_sounddevice_input_adapter_aborts_only_after_ring_rejects_a_callback() -> None:
    class CallbackAbort(Exception):
        pass

    class NativeInput:
        def __init__(self, **options: object) -> None:
            self.options = options

    class FakeSoundDevice:
        def __init__(self) -> None:
            self.CallbackAbort = CallbackAbort
            self.stream: NativeInput | None = None

        def RawInputStream(self, **options: object) -> NativeInput:
            self.stream = NativeInput(**options)
            return self.stream

    fake = FakeSoundDevice()
    accepted: list[tuple[bytes, bool]] = []

    def accept(frame: memoryview[int], status_present: bool) -> bool:
        accepted.append((bytes(frame), status_present))
        return not status_present

    backend = media_worker.SoundDeviceBackend(fake)
    backend.open_input_stream(
        sample_rate=16_000,
        channels=1,
        device="synthetic",
        blocksize=160,
        callback=accept,
    )
    assert fake.stream is not None
    callback = fake.stream.options["callback"]
    assert callable(callback)
    callback(b"\x01\x00", 1, object(), False)
    assert accepted == [(b"\x01\x00", False)]
    with pytest.raises(CallbackAbort):
        callback(b"\x01\x00", 1, object(), True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"maximum_wave_bytes": 43},
        {"maximum_recording_seconds": 0},
        {"maximum_recording_seconds": 120.1},
        {"transcription_timeout_seconds": 0},
        {"input_blocksize": -1},
        {"language": ""},
    ],
)
def test_media_worker_rejects_invalid_ptt_constructor_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        media_worker.MediaWorkerHandler(**kwargs)  # type: ignore[arg-type]


class _VoiceSupervisor:
    def __init__(
        self,
        *,
        block_start: bool = False,
        block_stop: bool = False,
        responses: Mapping[str, object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, tuple[object, ...], float | None]] = []
        self.started = False
        self.stopped = False
        self.block_start = block_start
        self.block_stop = block_stop
        self.responses = dict(responses or {})
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.stop_entered = asyncio.Event()
        self.release_stop = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def run_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        resources: Sequence[object] = (),
        hard_deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((job_id, job_kind, tuple(resources), hard_deadline_seconds))
        if job_kind == "media.record_start" and self.block_start:
            self.start_entered.set()
            await self.release_start.wait()
        if job_kind == "media.record_stop" and self.block_stop:
            self.stop_entered.set()
            await self.release_stop.wait()
        configured = self.responses.get(job_kind)
        if isinstance(configured, BaseException):
            raise configured
        if isinstance(configured, dict):
            return configured
        if job_kind == "media.record_start":
            return {"status": "recording"}
        if job_kind == "media.stt_preflight":
            return {"status": "ready"}
        if job_kind == "media.record_stop":
            return {
                "status": "transcribed",
                "text": "本地结果",
                "language": "zh",
                "segment_count": 2,
            }
        if job_kind == "media.record_cancel":
            return {"status": "cancelled"}
        raise AssertionError(job_kind)

    async def stop(self) -> None:
        self.stopped = True


def test_parent_voice_controller_transports_only_one_bounded_transcript_and_registry_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        paths = AppPaths(root=tmp_path / "Private App")
        root = paths.temp / "stt"
        root.mkdir(parents=True)
        registry = TempAssetRegistry(paths, directory_security=PortableDirectorySecurity())
        supervisor = _VoiceSupervisor()
        voice = MediaWorkerVoiceInput(
            roots={"stt_temp": root},
            maximum_recording_seconds=5,
            transcription_timeout_seconds=5,
            supervisor=supervisor,
            temp_registry=registry,
        )

        await voice.preflight()
        await voice.start()
        assert voice.state.value == "recording"
        assert len(registry.entries()) == 1
        result = await voice.stop()
        assert result.text == "本地结果"
        assert result.language == "zh"
        assert voice.state.value == "idle"
        assert registry.entries() == ()
        assert all(resources == () for _job, _kind, resources, _deadline in supervisor.calls)
        assert all(
            "pcm" not in kind and "wav" not in kind
            for _job, kind, _resources, _deadline in supervisor.calls
        )

        await voice.close()
        assert supervisor.stopped
        assert voice.state is VoiceCaptureState.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": " "},
        {"text": "bad\x00text"},
        {"text": "x" * 4_097},
        {"text": "ok", "language": ""},
        {"text": "ok", "language": "x" * 65},
        {"text": "ok", "segment_count": -1},
    ],
)
def test_parent_transcript_boundary_rejects_invalid_worker_metadata(
    kwargs: dict[str, object],
) -> None:
    candidate: dict[str, object] = {
        "text": "ok",
        "language": "zh",
        "segment_count": 1,
    }
    candidate.update(kwargs)
    with pytest.raises(ValueError):
        VoiceTranscription(**candidate)  # type: ignore[arg-type]
    assert VoiceTranscription("  本地文本 ", "zh", 1).text == "本地文本"


def test_parent_voice_protocol_helpers_reject_malformed_or_untrusted_payloads(
    tmp_path: Path,
) -> None:
    for payload, expected in (
        ({"status": "failed", "error_code": "stt_device_lost"}, "stt_device_lost"),
        ({"status": "failed", "error_code": ""}, "stt_worker_protocol"),
        ({"status": "unexpected"}, "stt_worker_protocol"),
    ):
        with pytest.raises(VoiceCaptureError) as caught:
            voice_module._require_recording_started(payload)
        assert caught.value.code == expected
        with pytest.raises(VoiceCaptureError) as caught:
            voice_module._require_stt_preflight(payload)
        assert caught.value.code == expected

    transcription_cases: tuple[tuple[object, str], ...] = (
        ({"status": "cancelled"}, "voice_cancelled"),
        ({"status": "failed", "error_code": "stt_process_failed"}, "stt_process_failed"),
        ({"status": "transcribed", "text": "x", "language": "zh"}, "stt_worker_protocol"),
        (
            {"status": "transcribed", "text": "x", "language": "zh", "segment_count": True},
            "stt_worker_protocol",
        ),
        (
            {"status": "transcribed", "text": "", "language": "zh", "segment_count": 1},
            "stt_worker_protocol",
        ),
    )
    for transcription_payload, expected in transcription_cases:
        with pytest.raises(VoiceCaptureError) as caught:
            voice_module._parse_transcription(transcription_payload)
        assert caught.value.code == expected

    class _WorkerError(RuntimeError):
        code = "worker_platform_unsupported"

    assert voice_module._voice_error_from_exception(_WorkerError()).code == "stt_worker_unavailable"
    assert voice_module._voice_error_from_exception(RuntimeError()).code == "stt_worker_failed"
    assert not voice_module._valid_duration(True, maximum=120)
    assert not voice_module._valid_duration(float("inf"), maximum=120)
    assert voice_module._valid_duration(120, maximum=120)

    root = tmp_path / "root"
    root.mkdir()
    directory = voice_module._recording_directory(root, "sttstart" + "a" * 32)
    assert directory.parent == root and directory.name.startswith("companion-recording-")
    directory.mkdir()
    voice_module._remove_directory(root, directory)
    assert not directory.exists()
    with pytest.raises(VoiceCaptureError):
        voice_module._remove_directory(root, tmp_path / "outside")


def test_parent_voice_start_and_preflight_failures_clean_up_and_remain_retryable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "stt"
        root.mkdir()
        supervisor = _VoiceSupervisor(
            responses={
                "media.stt_preflight": {"status": "failed", "error_code": "stt_runtime_missing"},
                "media.record_start": {"status": "failed", "error_code": "stt_microphone_denied"},
            }
        )
        voice = MediaWorkerVoiceInput(
            roots={"stt_temp": root},
            maximum_recording_seconds=5,
            transcription_timeout_seconds=5,
            supervisor=supervisor,
        )
        with pytest.raises(VoiceCaptureError) as caught:
            await voice.preflight()
        assert caught.value.code == "stt_runtime_missing"
        with pytest.raises(VoiceCaptureError) as caught:
            await voice.start()
        assert caught.value.code == "stt_microphone_denied"
        assert voice.state is VoiceCaptureState.idle
        assert [call[1] for call in supervisor.calls][-1] == "media.record_cancel"
        await voice.cancel()
        await voice.close()
        await voice.close()
        assert voice.state.value == "closed" and supervisor.stopped

    asyncio.run(scenario())


def test_voice_worker_command_includes_only_explicit_safe_configuration(tmp_path: Path) -> None:
    root = tmp_path / "private"
    command = voice_module._voice_worker_command(
        {"stt_temp": root},
        executable=tmp_path / "whisper-cli.exe",
        model=tmp_path / "model.bin",
        language="zh",
        threads=2,
        terminate_grace_seconds=0.5,
        max_audio_bytes=100,
        max_output_bytes=200,
        maximum_recording_seconds=120,
        transcription_timeout_seconds=60,
        input_device=3,
        input_blocksize=320,
    )
    assert "--stt-threads" in command and "--input-device-index" in command
    assert "--input-device-name" not in command and "pcm" not in " ".join(command).lower()

    named_command = voice_module._voice_worker_command(
        {"stt_temp": root},
        executable=tmp_path / "whisper-cli.exe",
        model=tmp_path / "model.bin",
        language="auto",
        threads=None,
        terminate_grace_seconds=0.5,
        max_audio_bytes=100,
        max_output_bytes=200,
        maximum_recording_seconds=120,
        transcription_timeout_seconds=60,
        input_device="合成麦克风",
        input_blocksize=0,
    )
    assert "--input-device-name" in named_command and "--stt-threads" not in named_command

    bool_device_command = voice_module._voice_worker_command(
        {"stt_temp": root},
        executable=tmp_path / "whisper-cli.exe",
        model=tmp_path / "model.bin",
        language="auto",
        threads=None,
        terminate_grace_seconds=0.5,
        max_audio_bytes=100,
        max_output_bytes=200,
        maximum_recording_seconds=120,
        transcription_timeout_seconds=60,
        input_device=True,
        input_blocksize=0,
    )
    assert "--input-device-index" not in bool_device_command
    assert "--input-device-name" not in bool_device_command


def test_parent_voice_lifecycle_rejects_invalid_states_and_cleans_cancelled_calls(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "stt"
        root.mkdir()
        with pytest.raises(ValueError):
            MediaWorkerVoiceInput(
                roots={"wrong": root},
                maximum_recording_seconds=5,
                transcription_timeout_seconds=5,
                supervisor=_VoiceSupervisor(),
            )
        with pytest.raises(ValueError):
            MediaWorkerVoiceInput(
                roots={"stt_temp": root},
                maximum_recording_seconds=True,
                transcription_timeout_seconds=5,
                supervisor=_VoiceSupervisor(),
            )

        supervisor = _VoiceSupervisor(block_start=True)
        voice = MediaWorkerVoiceInput(
            roots={"stt_temp": root},
            maximum_recording_seconds=5,
            transcription_timeout_seconds=5,
            supervisor=supervisor,
        )
        start_task = asyncio.create_task(voice.start())
        await asyncio.wait_for(supervisor.start_entered.wait(), timeout=1)
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert voice.state is VoiceCaptureState.idle
        assert [call[1] for call in supervisor.calls][-1] == "media.record_cancel"

        supervisor.block_start = False
        await voice.start()
        with pytest.raises(VoiceCaptureError) as caught:
            await voice.preflight()
        assert caught.value.code == "voice_invalid_state"
        await voice.cancel()
        with pytest.raises(VoiceCaptureError) as caught:
            await voice.stop()
        assert caught.value.code == "voice_invalid_state"
        await voice.close()
        with pytest.raises(VoiceCaptureError) as caught:
            await voice.preflight()
        assert caught.value.code == "voice_closed"

    asyncio.run(scenario())


def test_parent_voice_cancel_interrupts_an_inflight_transcription_and_lazy_factory(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "stt"
        root.mkdir()
        supervisor = _VoiceSupervisor(block_stop=True)
        prepared: list[str] = []
        voice = MediaWorkerVoiceInput(
            roots={"stt_temp": root},
            maximum_recording_seconds=5,
            transcription_timeout_seconds=5,
            supervisor_factory=lambda: supervisor,
            prepare_roots=lambda: prepared.append("prepared"),
        )
        await voice.preflight()
        assert prepared == ["prepared"] and supervisor.started
        await voice.start()
        stop_task = asyncio.create_task(voice.stop())
        await asyncio.wait_for(supervisor.stop_entered.wait(), timeout=1)
        await voice.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        assert voice.state is VoiceCaptureState.idle
        assert [call[1] for call in supervisor.calls].count("media.record_cancel") >= 1
        await voice.close()
        assert supervisor.stopped

    asyncio.run(scenario())


def test_parent_voice_controller_cancels_a_start_that_races_with_release(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "stt"
        root.mkdir()
        supervisor = _VoiceSupervisor(block_start=True)
        voice = MediaWorkerVoiceInput(
            roots={"stt_temp": root},
            maximum_recording_seconds=5,
            transcription_timeout_seconds=5,
            supervisor=supervisor,
        )
        start_task = asyncio.create_task(voice.start())
        await asyncio.wait_for(supervisor.start_entered.wait(), timeout=1)
        await voice.cancel()
        supervisor.release_start.set()
        with pytest.raises(VoiceCaptureError) as caught:
            await asyncio.wait_for(start_task, timeout=1)
        assert caught.value.code == "voice_invalid_state"
        assert voice.state is VoiceCaptureState.idle
        assert [call[1] for call in supervisor.calls] == [
            "media.record_start",
            "media.record_cancel",
        ]
        await voice.close()

    asyncio.run(scenario())


def test_parent_voice_controller_timeout_cancels_the_worker_without_continuous_listening(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "stt"
        root.mkdir()
        elapsed = asyncio.Event()

        async def wait_for_limit(_seconds: float) -> None:
            await elapsed.wait()

        supervisor = _VoiceSupervisor()
        voice = MediaWorkerVoiceInput(
            roots={"stt_temp": root},
            maximum_recording_seconds=5,
            transcription_timeout_seconds=5,
            supervisor=supervisor,
            watchdog_wait=wait_for_limit,
        )
        await voice.start()
        elapsed.set()
        for _ in range(100):
            if voice.state is VoiceCaptureState.timed_out:
                break
            await asyncio.sleep(0)
        assert voice.state is VoiceCaptureState.timed_out
        with pytest.raises(VoiceCaptureError) as caught:
            await voice.stop()
        assert caught.value.code == "stt_recording_too_long"
        assert [call[1] for call in supervisor.calls].count("media.record_cancel") >= 1
        await voice.close()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="W18 Job Object process-tree proof is Windows-only")
def test_media_worker_job_closure_reaps_whisper_version_probe_child_tree(tmp_path: Path) -> None:
    """A whisper child inherits MediaWorker's Job Object and cannot outlive it."""

    # This integration belongs to the Windows-only layer.  The direct runner's
    # terminate/kill handles one whisper process; W12's Job Object owns any
    # descendants that CLI itself launches.
    async def scenario() -> None:
        root = tmp_path / "STT 临时 根"
        root.mkdir()
        marker = tmp_path / "child.pid"
        script = tmp_path / "fake whisper child.py"
        script.write_text(
            "\n".join(
                (
                    "import subprocess, sys, time",
                    "from pathlib import Path",
                    f"marker = Path({str(marker)!r})",
                    "if sys.argv[-1] == '--version':",
                    "    child = subprocess.Popen(",
                    "        [sys.executable, '-c', 'import time; time.sleep(60)']",
                    "    )",
                    "    marker.write_text(str(child.pid), encoding='ascii')",
                    "    raise SystemExit(0)",
                    "raise SystemExit(9)",
                )
            ),
            encoding="utf-8",
        )
        model = tmp_path / "model.bin"
        model.write_bytes(b"synthetic model")
        supervisor = WorkerSupervisor(
            name="media_stt_tree",
            role="media",
            command=(
                str(Path(sys.executable).resolve()),
                "-m",
                "app.media_entrypoint",
                "--root",
                f"stt_temp={root}",
                "--stt-executable",
                str(Path(sys.executable).resolve()),
                "--stt-model",
                str(model),
                "--stt-temporary-root",
                str(root),
                "--stt-executable-prefix",
                str(script),
            ),
            adapter=process_adapter_for_current_platform(),
            resource_policy=ApprovedResourcePolicy(roots={"stt_temp": root}),
            config=SupervisorConfig(maximum_active_jobs=1, maximum_job_seconds=8),
        )
        try:
            await supervisor.start()
            assert await supervisor.run_job(
                job_id="stt-preflight-tree",
                job_kind="media.stt_preflight",
                hard_deadline_seconds=5,
            ) == {"status": "ready"}
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            assert marker.exists()
            child_pid = int(marker.read_text(encoding="ascii"))
        finally:
            await supervisor.stop()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _windows_pid_exists(child_pid):
            await asyncio.sleep(0.02)
        assert not _windows_pid_exists(child_pid)

    asyncio.run(scenario())


def _windows_pid_exists(pid: int) -> bool:
    # ``tasklist`` avoids optional third-party process libraries in the Windows
    # Job Object test and only returns the numeric PID, never user content.
    import subprocess

    result = subprocess.run(
        ["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        check=False,
        capture_output=True,
        text=True,
    )
    return f'"{pid}"' in result.stdout
