"""Synthetic W17 MediaWorker behavior tests; no physical audio device is used."""

from __future__ import annotations

import asyncio
import os
import threading
import wave
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.config import Settings
from app.config.settings import PipelineConfig
from app.core import CancellationToken
from app.media import client as media_client
from app.media.client import (
    MediaWorkerAudioPlayer,
    _parse_device_list,
    _parse_playback_result,
    _worker_command,
    _worker_failure,
    create_media_worker_audio_player,
)
from app.media.types import (
    MAX_OUTPUT_DEVICES,
    AudioOutputDevice,
    OutputDeviceList,
    clean_device_label,
    output_device_id,
)
from app.media.worker import (
    MediaWorkerHandler,
    SoundDeviceBackend,
    _choose_output,
    _close_unowned_stream,
    _default_output_index,
    _DiscoveredOutput,
    _selectable_outputs,
    _WaveFormat,
)
from app.paths import AppPaths
from app.pipelines.audio_player import AudioPlaybackResult
from app.schemas import AudioResult
from app.workers import WorkerError
from app.workers.access import AuthorizedResource, ResourceReference


class _FakeStream:
    def __init__(self, *, blocks_write: bool = False, fails_write: bool = False) -> None:
        self.active = False
        self.blocks_write = blocks_write
        self.fails_write = fails_write
        self.start_count = 0
        self.write_count = 0
        self.stop_count = 0
        self.abort_count = 0
        self.close_count = 0
        self.write_started = threading.Event()
        self.release_write = threading.Event()

    def start(self) -> None:
        self.active = True
        self.start_count += 1

    def write(self, _frames: bytes) -> None:
        self.write_count += 1
        self.write_started.set()
        if self.blocks_write and not self.release_write.wait(timeout=2):
            raise RuntimeError("synthetic_media_write_blocked")
        if self.fails_write:
            raise OSError("synthetic_media_device_lost")

    def abort(self) -> None:
        self.active = False
        self.abort_count += 1
        self.release_write.set()

    def stop(self) -> None:
        self.active = False
        self.stop_count += 1

    def close(self) -> None:
        self.active = False
        self.close_count += 1


class _FakeBackend:
    def __init__(self, discovered: tuple[_DiscoveredOutput, ...]) -> None:
        self.discovered = discovered
        self.fail_low_latency = False
        self.fail_all_opens = False
        self.block_writes = False
        self.fail_writes = False
        self.open_calls: list[tuple[int, str]] = []
        self.streams: list[_FakeStream] = []

    def output_devices(self) -> tuple[_DiscoveredOutput, ...]:
        return self.discovered

    def open_output_stream(
        self,
        _audio_format: _WaveFormat,
        *,
        native_index: int,
        latency: str,
    ) -> _FakeStream:
        self.open_calls.append((native_index, latency))
        if self.fail_all_opens or (latency == "low" and self.fail_low_latency):
            raise OSError("synthetic_media_device_occupied")
        stream = _FakeStream(blocks_write=self.block_writes, fails_write=self.fail_writes)
        self.streams.append(stream)
        return stream


class _BrokenBackend:
    def output_devices(self) -> tuple[_DiscoveredOutput, ...]:
        raise OSError("synthetic_enumeration_failure")

    def open_output_stream(
        self,
        _audio_format: _WaveFormat,
        *,
        native_index: int,
        latency: str,
    ) -> _FakeStream:
        del native_index, latency
        raise OSError("synthetic_open_failure")


class _FakeNativeAudio:
    def __init__(self) -> None:
        self.default = SimpleNamespace(device=(None, 4))
        self.raw_calls: list[dict[str, object]] = []
        self.created_stream = _FakeStream()

    def query_devices(self) -> list[dict[str, object]]:
        return [
            {
                "max_output_channels": 0,
                "name": "Input only",
                "hostapi": 0,
                "default_samplerate": 48_000,
            },
            {
                "max_output_channels": 2,
                "name": "  Desk\x00 speakers  ",
                "hostapi": 0,
                "default_samplerate": 48_000.0,
            },
            {
                "max_output_channels": "bad",
                "name": "Malformed",
                "hostapi": 0,
                "default_samplerate": 48_000,
            },
            {
                "max_output_channels": 2,
                "name": "Missing host API",
                "hostapi": 4,
                "default_samplerate": 48_000,
            },
            {
                "max_output_channels": 2,
                "name": "Default output",
                "hostapi": 0,
                "default_samplerate": 44_100,
            },
        ]

    def query_hostapis(self) -> list[dict[str, str]]:
        return [{"name": "Synthetic PortAudio"}]

    def RawOutputStream(self, **options: object) -> _FakeStream:
        self.raw_calls.append(options)
        return self.created_stream


class _ResultSupervisor:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.payload = payload or {"status": "played", "notice_code": ""}
        self.error = error
        self.started = False
        self.stopped = False
        self.calls: list[tuple[str, str, tuple[ResourceReference, ...]]] = []

    async def start(self) -> None:
        self.started = True

    async def run_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        resources: Sequence[ResourceReference] = (),
        hard_deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        del hard_deadline_seconds
        self.calls.append((job_id, job_kind, tuple(resources)))
        if self.error is not None:
            raise self.error
        return self.payload

    async def cancel_job(self, _job_id: str, *, deadline_at: float | None = None) -> None:
        del deadline_at

    async def stop(self) -> None:
        self.stopped = True


class _HangingSupervisor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled_jobs: list[str] = []
        self.stopped = False

    async def start(self) -> None:
        return None

    async def run_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        resources: Sequence[ResourceReference] = (),
        hard_deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        del job_id, job_kind, resources, hard_deadline_seconds
        self.started.set()
        await self.release.wait()
        return {"status": "cancelled"}

    async def cancel_job(self, job_id: str, *, deadline_at: float | None = None) -> None:
        del deadline_at
        self.cancelled_jobs.append(job_id)
        self.release.set()

    async def stop(self) -> None:
        self.stopped = True


def _output(name: str, *, native_index: int, is_default: bool = False) -> _DiscoveredOutput:
    return _DiscoveredOutput(
        device=AudioOutputDevice(
            device_id=output_device_id(
                host_api_name="SyntheticHost",
                device_name=name,
                maximum_output_channels=2,
                default_sample_rate=48_000,
            ),
            label=name,
            is_default=is_default,
        ),
        native_index=native_index,
    )


def _open_wave_resource(tmp_path: Path) -> tuple[Path, AuthorizedResource]:
    path = tmp_path / "synthetic.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\0\0" * 160)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    return path, AuthorizedResource("wave", os.open(path, flags), owns_descriptor=True)


def _audio_result(path: Path) -> AudioResult:
    return AudioResult(
        job_id="job_media",
        turn_id="turn_media",
        segment_id="segment_media",
        success=True,
        audio_path=path,
        duration_ms=10,
    )


def test_media_worker_lists_only_unique_bounded_index_independent_devices() -> None:
    default = _output("Default output", native_index=7, is_default=True)
    selected = _output("A selected output", native_index=11)
    duplicate_one = _output("Ambiguous endpoint", native_index=2)
    duplicate_two = _output("Ambiguous endpoint", native_index=3)
    extra = tuple(_output(f"Output {index}", native_index=20 + index) for index in range(20))
    backend = _FakeBackend((default, selected, duplicate_one, duplicate_two, *extra))
    handler = MediaWorkerHandler(backend=backend)

    payload = asyncio.run(handler.run_job("media.devices", (), asyncio.Event()))

    devices = payload["devices"]
    assert payload["status"] == "ok"
    assert payload["truncated"] is True
    assert isinstance(devices, list) and len(devices) == MAX_OUTPUT_DEVICES
    ids = [item["device_id"] for item in devices]
    assert default.device.device_id in ids
    assert selected.device.device_id in ids
    assert duplicate_one.device.device_id not in ids
    reindexed = _output("A selected output", native_index=1)
    assert reindexed.device.device_id == selected.device.device_id


def test_media_worker_falls_back_after_selected_device_disappears_and_low_latency_is_busy(
    tmp_path: Path,
) -> None:
    default = _output("Default output", native_index=1, is_default=True)
    selected = _output("USB headset", native_index=2)
    path, resource = _open_wave_resource(tmp_path)
    backend = _FakeBackend((default, selected))
    handler = MediaWorkerHandler(backend=backend, selected_device_id=selected.device.device_id)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        try:
            initial = await handler.run_job("media.play", (resource,), asyncio.Event())
            backend.discovered = (default,)
            fallback = await handler.run_job("media.play", (resource,), asyncio.Event())
            await handler.close()

            busy_backend = _FakeBackend((default,))
            busy_backend.fail_low_latency = True
            busy_handler = MediaWorkerHandler(backend=busy_backend)
            try:
                latency_fallback = await busy_handler.run_job(
                    "media.play", (resource,), asyncio.Event()
                )
            finally:
                await busy_handler.close()
            assert busy_backend.open_calls == [(1, "low"), (1, "high")]
            return initial, fallback, latency_fallback
        finally:
            resource.close()

    initial, fallback, latency_fallback = asyncio.run(scenario())

    assert path.exists()
    assert initial == {"status": "played", "notice_code": ""}
    assert fallback == {"status": "played", "notice_code": "audio_device_fallback"}
    assert latency_fallback == {"status": "played", "notice_code": "audio_latency_fallback"}
    assert backend.open_calls == [(2, "low"), (1, "low")]
    path.unlink()
    assert not path.exists()


def test_media_worker_releases_lost_or_cancelled_streams_without_holding_the_wave(
    tmp_path: Path,
) -> None:
    default = _output("Default output", native_index=1, is_default=True)
    path, resource = _open_wave_resource(tmp_path)
    lost_backend = _FakeBackend((default,))
    lost_backend.fail_writes = True
    lost_handler = MediaWorkerHandler(backend=lost_backend)

    async def scenario() -> dict[str, Any]:
        try:
            lost = await lost_handler.run_job("media.play", (resource,), asyncio.Event())
            assert lost_backend.streams[0].abort_count == 1
            assert lost_backend.streams[0].close_count == 1

            blocked_backend = _FakeBackend((default,))
            blocked_backend.block_writes = True
            blocked_handler = MediaWorkerHandler(backend=blocked_backend)
            cancellation = asyncio.Event()
            task = asyncio.create_task(
                blocked_handler.run_job("media.play", (resource,), cancellation)
            )
            while not blocked_backend.streams:
                await asyncio.sleep(0)
            assert await asyncio.to_thread(blocked_backend.streams[0].write_started.wait, 1)
            cancellation.set()
            cancelled = await asyncio.wait_for(task, timeout=1)
            await blocked_handler.close()
            assert blocked_backend.streams[0].abort_count == 1
            assert blocked_backend.streams[0].close_count == 1
            return {"lost": lost, "cancelled": cancelled}
        finally:
            resource.close()
            await lost_handler.close()

    payloads = asyncio.run(scenario())

    assert payloads["lost"] == {"status": "skipped", "error_code": "audio_device_lost"}
    assert payloads["cancelled"] == {"status": "cancelled"}
    path.unlink()
    assert not path.exists()


def test_media_worker_player_keeps_paths_out_of_jobs_and_maps_worker_failures(
    tmp_path: Path,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    path = root / "segment.wav"
    supervisor = _ResultSupervisor({"status": "played", "notice_code": "audio_device_fallback"})
    player = MediaWorkerAudioPlayer(
        roots={"audio_temp": root},
        selected_device_id=None,
        supervisor=supervisor,
    )

    async def scenario() -> tuple[AudioPlaybackResult, tuple[ResourceReference, ...]]:
        result = await player.play(_audio_result(path), CancellationToken("turn_media"))
        await player.close()
        return result, supervisor.calls[0][2]

    result, resources = asyncio.run(scenario())

    assert result.played and result.notice_code == "audio_device_fallback"
    assert resources == (
        ResourceReference(resource_id="wave", root_id="audio_temp", relative_path="segment.wav"),
    )
    assert resources[0].relative_path is not None
    assert str(root) not in resources[0].relative_path
    assert supervisor.stopped

    failing = _ResultSupervisor(error=WorkerError("worker_job_deadline"))
    failing_player = MediaWorkerAudioPlayer(
        roots={"audio_temp": root},
        selected_device_id=None,
        supervisor=failing,
    )
    failed = asyncio.run(failing_player.play(_audio_result(path), CancellationToken("turn_failed")))
    assert not failed.played and failed.error_code == "audio_worker_hung"
    asyncio.run(failing_player.close())
    assert failing.stopped


def test_media_worker_player_stop_settles_a_hung_job_without_waiting_for_native_io(
    tmp_path: Path,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    path = root / "segment.wav"
    supervisor = _HangingSupervisor()
    player = MediaWorkerAudioPlayer(
        roots={"audio_temp": root},
        selected_device_id=None,
        supervisor=supervisor,
    )

    async def scenario() -> None:
        playback = asyncio.create_task(
            player.play(_audio_result(path), CancellationToken("turn_hang"))
        )
        await asyncio.wait_for(supervisor.started.wait(), timeout=1)
        await player.stop(immediate=True)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(playback, timeout=1)
        assert len(supervisor.cancelled_jobs) == 1
        await player.close()
        assert supervisor.stopped

    asyncio.run(scenario())


def test_parent_media_imports_do_not_load_the_native_audio_binding() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative_path in (
        "app/bootstrap.py",
        "app/media/__init__.py",
        "app/media/client.py",
        "app/pipelines/audio_player.py",
    ):
        assert "import sounddevice" not in (root / relative_path).read_text(encoding="utf-8")
    assert "import sounddevice" in (root / "app/media/worker.py").read_text(encoding="utf-8")


def test_audio_output_contract_normalizes_identity_and_rejects_invalid_values() -> None:
    stable = output_device_id(
        host_api_name="Synthetic host",
        device_name="Desk speakers",
        maximum_output_channels=2,
        default_sample_rate=48_000.0,
    )
    assert stable == output_device_id(
        host_api_name="Synthetic host",
        device_name="Desk speakers",
        maximum_output_channels=2,
        default_sample_rate=48_000,
    )
    assert clean_device_label("  Desk\x00\t speakers  ") == "Desk speakers"
    assert clean_device_label("x" * 200) == "x" * 160

    valid = AudioOutputDevice(device_id=stable, label="Desk speakers", is_default=False)
    with pytest.raises(ValueError, match="id"):
        AudioOutputDevice(device_id="audio_not_hex", label="Desk speakers", is_default=False)
    with pytest.raises(ValueError, match="label"):
        AudioOutputDevice(device_id=stable, label="\x00", is_default=False)
    with pytest.raises(ValueError, match="default"):
        AudioOutputDevice(device_id=stable, label="Desk speakers", is_default=cast(bool, 1))
    with pytest.raises(ValueError, match="list"):
        OutputDeviceList((valid,) * (MAX_OUTPUT_DEVICES + 1), False)
    with pytest.raises(ValueError, match="truncation"):
        OutputDeviceList((), cast(bool, 1))
    with pytest.raises(ValueError, match="reason"):
        OutputDeviceList((), False, "not a stable reason")
    with pytest.raises(ValueError, match="reason"):
        OutputDeviceList((), False, cast(str, 1))


def test_sounddevice_backend_filters_native_metadata_and_opens_raw_stream() -> None:
    native = _FakeNativeAudio()
    backend = SoundDeviceBackend(native)

    discovered = backend.output_devices()
    stream = backend.open_output_stream(
        _WaveFormat(sample_rate=16_000, channels=1, dtype="int16"),
        native_index=4,
        latency="low",
    )

    assert [(item.device.label, item.native_index) for item in discovered] == [
        ("Desk speakers", 1),
        ("Default output", 4),
    ]
    assert discovered[-1].device.is_default
    assert stream is native.created_stream
    assert native.raw_calls == [
        {
            "samplerate": 16_000,
            "channels": 1,
            "dtype": "int16",
            "device": 4,
            "latency": "low",
        }
    ]
    assert _default_output_index((0, 3)) == 3
    assert _default_output_index((0, -1)) is None
    assert _default_output_index((0, True)) is None
    assert _default_output_index("0,3") is None


def test_media_worker_protocol_paths_and_high_latency_stream_reuse(tmp_path: Path) -> None:
    default = _output("Default output", native_index=1, is_default=True)
    path, resource = _open_wave_resource(tmp_path)
    backend = _FakeBackend((default,))
    handler = MediaWorkerHandler(backend=backend)
    busy_backend = _FakeBackend((default,))
    busy_backend.fail_low_latency = True
    busy_handler = MediaWorkerHandler(backend=busy_backend)

    async def scenario() -> None:
        invalid = AuthorizedResource("not_wave", 0)
        assert await handler.run_job("media.unknown", (), asyncio.Event()) == {
            "status": "skipped",
            "error_code": "audio_protocol_invalid",
        }
        assert await handler.run_job("media.release", (invalid,), asyncio.Event()) == {
            "status": "skipped",
            "error_code": "audio_protocol_invalid",
        }

        cancelled = asyncio.Event()
        cancelled.set()
        assert await handler.run_job("media.play", (resource,), cancelled) == {
            "status": "cancelled"
        }

        first = await busy_handler.run_job("media.play", (resource,), asyncio.Event())
        second = await busy_handler.run_job("media.play", (resource,), asyncio.Event())
        assert first == {"status": "played", "notice_code": "audio_latency_fallback"}
        assert second == {"status": "played", "notice_code": "audio_latency_fallback"}
        assert busy_backend.open_calls == [(1, "low"), (1, "high")]

        released = await handler.run_job("media.release", (), asyncio.Event())
        assert released == {"status": "released"}
        await handler.close()
        await busy_handler.close()

    try:
        asyncio.run(scenario())
    finally:
        resource.close()
    assert path.exists()

    selectable, duplicate_ids = _selectable_outputs((default, default))
    assert selectable == ()
    assert duplicate_ids == frozenset({default.device.device_id})
    with pytest.raises(RuntimeError, match="audio_device_unavailable"):
        _choose_output((), None)

    graceful = _FakeStream()
    graceful.start()
    _close_unowned_stream(graceful)
    immediate = _FakeStream()
    immediate.start()
    _close_unowned_stream(immediate, immediate=True)
    _close_unowned_stream(None)
    assert (graceful.stop_count, graceful.abort_count, graceful.close_count) == (1, 0, 1)
    assert (immediate.stop_count, immediate.abort_count, immediate.close_count) == (0, 1, 1)


def test_media_worker_classifies_unavailable_devices_and_invalid_wave_assets(
    tmp_path: Path,
) -> None:
    default = _output("Default output", native_index=1, is_default=True)
    wave_path, wave_resource = _open_wave_resource(tmp_path)
    malformed_path = tmp_path / "malformed.wav"
    malformed_path.write_bytes(b"not a wave")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    malformed_resource = AuthorizedResource("wave", os.open(malformed_path, flags), True)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        unavailable = await MediaWorkerHandler(backend=_FakeBackend(())).run_job(
            "media.play", (wave_resource,), asyncio.Event()
        )
        malformed = await MediaWorkerHandler(backend=_FakeBackend((default,))).run_job(
            "media.play", (malformed_resource,), asyncio.Event()
        )
        oversized = await MediaWorkerHandler(
            backend=_FakeBackend((default,)), maximum_wave_bytes=44
        ).run_job("media.play", (wave_resource,), asyncio.Event())
        return unavailable, malformed, oversized

    try:
        unavailable, malformed, oversized = asyncio.run(scenario())
    finally:
        wave_resource.close()
        malformed_resource.close()

    assert unavailable == {"status": "skipped", "error_code": "audio_device_unavailable"}
    assert malformed == {"status": "skipped", "error_code": "audio_wave_invalid"}
    assert oversized == {"status": "skipped", "error_code": "audio_result_too_large"}
    assert wave_path.exists()
    broken_payload = asyncio.run(
        MediaWorkerHandler(backend=_BrokenBackend()).run_job("media.devices", (), asyncio.Event())
    )
    assert broken_payload == {
        "status": "unavailable",
        "devices": [],
        "truncated": False,
    }


def test_media_client_validates_worker_protocol_and_degrades_failures(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    path = root / "segment.wav"
    device = _output("Default output", native_index=1, is_default=True).device
    device_payload = {
        "status": "ok",
        "devices": [
            {
                "device_id": device.device_id,
                "label": device.label,
                "is_default": True,
            }
        ],
        "truncated": False,
    }
    supervisor = _ResultSupervisor(device_payload)
    player = MediaWorkerAudioPlayer(
        roots={"audio_temp": root},
        selected_device_id=None,
        supervisor=supervisor,
    )

    async def scenario() -> tuple[OutputDeviceList, AudioPlaybackResult, AudioPlaybackResult]:
        listed = await player.list_output_devices()
        unavailable = await player.play(
            AudioResult(
                job_id="job_failed",
                turn_id="turn_failed",
                segment_id="segment_failed",
                success=False,
                error_code="tts_failed",
            ),
            CancellationToken("turn_failed"),
        )
        unapproved = await player.play(
            _audio_result(tmp_path / "outside.wav"), CancellationToken("turn_outside")
        )
        await player.stop()
        await player.close()
        return listed, unavailable, unapproved

    listed, unavailable, unapproved = asyncio.run(scenario())
    assert listed == OutputDeviceList((device,), False)
    assert unavailable == AudioPlaybackResult(played=False, error_code="audio_unavailable")
    assert unapproved == AudioPlaybackResult(played=False, error_code="audio_path_unapproved")
    assert [call[1] for call in supervisor.calls] == [
        "media.devices",
        "media.release",
        "media.release",
    ]
    assert supervisor.stopped

    malformed_devices: tuple[dict[str, object], ...] = (
        {},
        {"status": "ok", "devices": [], "truncated": "no"},
    )
    assert all(
        _parse_device_list(payload)
        == OutputDeviceList((), False, "audio_device_enumeration_failed")
        for payload in malformed_devices
    )
    assert _parse_playback_result({"status": "cancelled"}) == AudioPlaybackResult(
        played=False, error_code="audio_cancelled"
    )
    assert _parse_playback_result({"status": "played", "notice_code": 3}) == AudioPlaybackResult(
        played=False, error_code="audio_worker_protocol"
    )
    assert _parse_playback_result({"status": "skipped", "error_code": ""}) == AudioPlaybackResult(
        played=False, error_code="audio_worker_protocol"
    )
    assert _worker_failure(WorkerError("worker_platform_unsupported")).error_code == (
        "audio_worker_unavailable"
    )
    assert _worker_failure(WorkerError("worker_heartbeat_lost")).error_code == "audio_worker_hung"
    assert _worker_failure(RuntimeError("unexpected")).error_code == "audio_worker_failed"

    generic_failure = _ResultSupervisor(error=RuntimeError("synthetic_worker_error"))
    failed_player = MediaWorkerAudioPlayer(
        roots={"audio_temp": root},
        selected_device_id=None,
        supervisor=generic_failure,
    )
    failed_listing = asyncio.run(failed_player.list_output_devices())
    failure = asyncio.run(
        failed_player.play(_audio_result(path), CancellationToken("turn_generic"))
    )
    assert failed_listing == OutputDeviceList((), False, "audio_device_enumeration_failed")
    assert failure == AudioPlaybackResult(played=False, error_code="audio_worker_failed")
    asyncio.run(failed_player.close())


def test_media_player_factories_prepare_only_approved_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeDirectorySecurity:
        def ensure_private_tree(self, _root: Path, children: tuple[Path, ...]) -> None:
            for child in children:
                child.mkdir(parents=True, exist_ok=True)

    class FakeWorkerSupervisor(_ResultSupervisor):
        instances: list[FakeWorkerSupervisor] = []

        def __init__(self, **options: object) -> None:
            super().__init__()
            self.options = options
            self.instances.append(self)

    monkeypatch.setattr(
        media_client,
        "directory_security_for_current_platform",
        FakeDirectorySecurity,
    )
    monkeypatch.setattr(media_client, "WorkerSupervisor", FakeWorkerSupervisor)
    settings = Settings(pipeline=PipelineConfig(playback_mode="system"))
    settings._paths = AppPaths(root=tmp_path / "private")

    production = create_media_worker_audio_player(settings)
    review = MediaWorkerAudioPlayer.for_review(tmp_path / "review")

    async def scenario() -> None:
        await production._ensure_supervisor()
        await review._ensure_supervisor()
        await production.close()
        await review.close()

    asyncio.run(scenario())

    assert (tmp_path / "private" / "temp" / "audio").is_dir()
    assert settings.paths.audio_cache.is_dir()
    assert (tmp_path / "review").is_dir()
    commands = [
        cast(tuple[str, ...], item.options["command"]) for item in FakeWorkerSupervisor.instances
    ]
    assert all(command[1:3] == ("-m", "app.media_entrypoint") for command in commands)
    assert any("audio_temp=" in item for item in commands[0])
    assert any("audio_cache=" in item for item in commands[0])
    assert _worker_command({"audio_temp": tmp_path}, "audio_" + "a" * 32)[-2:] == (
        "--output-device-id",
        "audio_" + "a" * 32,
    )

    with pytest.raises(ValueError, match="exactly one"):
        MediaWorkerAudioPlayer(
            roots={"audio_temp": tmp_path},
            selected_device_id=None,
            supervisor=_ResultSupervisor(),
            supervisor_factory=_ResultSupervisor,
        )
    with pytest.raises(ValueError, match="approved audio roots"):
        MediaWorkerAudioPlayer(roots={}, selected_device_id=None, supervisor=_ResultSupervisor())
