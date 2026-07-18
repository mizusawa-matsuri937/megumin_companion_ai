"""Lifecycle, fault, cancellation, privacy, and resource-bound tests for W12."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from typing import Any

import pytest
from app.workers.process import ManagedProcess, ProcessAdapterError, UnsupportedProcessAdapter
from app.workers.protocol import FrameDecoder, HelperMessage, ProtocolError, encode_message
from app.workers.supervisor import (
    MAX_STDERR_BYTES,
    SupervisorConfig,
    WorkerActualState,
    WorkerError,
    WorkerSupervisor,
)

_SENTINEL = "W12_BODY_PCM_OCR_PATH_SECRET_C:\\Users\\private\\voice.wav"


class _FakeJobProcess:
    """Explicit test double; it is never used as Windows Job evidence."""

    def __init__(
        self,
        *,
        role: str = "media",
        create_no_window: bool = True,
        send_hello: bool = True,
        heartbeat: bool = True,
        stderr: bytes = b"",
        crash_after_handshake: bool = False,
        malformed_after_handshake: bytes | None = None,
        ignore_shutdown: bool = False,
        fail_write: bool = False,
        fail_query: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.pid = 1234
        self.create_no_window = create_no_window
        self._role = role
        self._stdout: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._stderr: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._decoder = FrameDecoder()
        self._exit = asyncio.get_running_loop().create_future()
        self._heartbeat_enabled = heartbeat
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._crash_after_handshake = crash_after_handshake
        self._malformed_after_handshake = malformed_after_handshake
        self._ignore_shutdown = ignore_shutdown
        self._fail_write = fail_write
        self._fail_query = fail_query
        self._fail_close = fail_close
        self.terminated = False
        self.closed = False
        self.active = 1
        self.cancelled_jobs: list[str] = []
        self.received_types: list[str] = []
        if send_hello:
            self._stdout.put_nowait(
                encode_message(HelperMessage(message_type="hello", payload={"role": role}))
            )
        if stderr:
            for offset in range(0, len(stderr), 8192):
                self._stderr.put_nowait(stderr[offset : offset + 8192])
            self._stderr.put_nowait(None)

    async def write_stdin(self, data: bytes) -> None:
        if self._fail_write:
            raise OSError("synthetic write failure")
        for message in self._decoder.feed(data):
            self.received_types.append(message.message_type)
            if message.message_type == "handshake.accepted":
                if self._malformed_after_handshake is not None:
                    self._stdout.put_nowait(self._malformed_after_handshake)
                if self._heartbeat_enabled:
                    self._heartbeat_task = asyncio.create_task(self._heartbeats())
                if self._crash_after_handshake:
                    asyncio.get_running_loop().call_soon(self._finish, 17)
            elif message.message_type == "job.start":
                if message.payload["job_kind"] == "complete":
                    self._stdout.put_nowait(
                        encode_message(
                            HelperMessage(
                                message_type="job.completed",
                                request_id=message.request_id,
                                payload={"status": "ok"},
                            )
                        )
                    )
            elif message.message_type == "job.cancel" and message.request_id is not None:
                self.cancelled_jobs.append(message.request_id)
                self._stdout.put_nowait(
                    encode_message(
                        HelperMessage(
                            message_type="job.cancelled",
                            request_id=message.request_id,
                            payload={},
                        )
                    )
                )
            elif message.message_type == "shutdown":
                if self._ignore_shutdown:
                    continue
                self._stdout.put_nowait(
                    encode_message(HelperMessage(message_type="stopped", payload={}))
                )
                self._finish(0)

    async def _heartbeats(self) -> None:
        sequence = 0
        while not self._exit.done():
            await asyncio.sleep(0.01)
            sequence += 1
            self._stdout.put_nowait(
                encode_message(
                    HelperMessage(message_type="heartbeat", payload={"sequence": sequence})
                )
            )

    async def read_stdout(self, max_bytes: int) -> bytes:
        del max_bytes
        item = await self._stdout.get()
        return b"" if item is None else item

    async def read_stderr(self, max_bytes: int) -> bytes:
        del max_bytes
        item = await self._stderr.get()
        return b"" if item is None else item

    async def wait(self) -> int:
        return await asyncio.shield(self._exit)

    async def terminate_tree(self, exit_code: int = 1) -> None:
        self.terminated = True
        self._finish(exit_code)

    async def active_process_count(self) -> int | None:
        if self._fail_query:
            raise OSError("synthetic query failure")
        return self.active

    async def close(self) -> None:
        self.closed = True
        self._finish(0)
        if self._fail_close:
            raise OSError("synthetic close failure")

    def _finish(self, code: int) -> None:
        if not self._exit.done():
            self.active = 0
            self._exit.set_result(code)
            self._stdout.put_nowait(None)
            self._stderr.put_nowait(None)
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()


class _FakeJobAdapter:
    """Explicit fake with configurable per-spawn behavior."""

    supports_job_objects = True

    def __init__(
        self,
        factories: Sequence[dict[str, Any]] | None = None,
        *,
        spawn_error: bool = False,
    ) -> None:
        self._factories = list(factories or ({},))
        self._spawn_error = spawn_error
        self.processes: list[_FakeJobProcess] = []

    async def spawn(
        self,
        command: Sequence[str],
        *,
        inherited_handles: Sequence[int] = (),
    ) -> ManagedProcess:
        assert command == ("trusted-helper",)
        assert not inherited_handles
        if self._spawn_error:
            raise ProcessAdapterError("worker_synthetic_spawn_failed")
        config = self._factories.pop(0) if self._factories else {"crash_after_handshake": True}
        process = _FakeJobProcess(**config)
        self.processes.append(process)
        return process


def _config(**overrides: Any) -> SupervisorConfig:
    values: dict[str, Any] = {
        "handshake_timeout_seconds": 0.2,
        "heartbeat_timeout_seconds": 0.08,
        "soft_cancel_grace_seconds": 0.03,
        "terminate_wait_seconds": 0.1,
        "maximum_job_seconds": 0.2,
        "restart_backoff_initial_seconds": 0.01,
        "restart_backoff_max_seconds": 0.02,
    }
    values.update(overrides)
    return SupervisorConfig(**values)


def test_successful_handshake_job_and_orderly_shutdown() -> None:
    async def scenario() -> None:
        scavenges: list[str] = []
        adapter = _FakeJobAdapter()
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            temp_scavenger=lambda: scavenges.append("done"),
            config=_config(),
        )
        await supervisor.start()
        assert supervisor.actual_state is WorkerActualState.enabled
        assert await supervisor.run_job(job_id="job-1", job_kind="complete") == {"status": "ok"}
        report = await supervisor.stop()
        assert report.hard_terminated is False
        assert report.active_processes == 0
        assert report.exit_code == 0 and report.process_close_succeeded
        assert report.scavenge_succeeded and report.deadline_met
        assert scavenges == ["done"]
        assert adapter.processes[0].received_types == [
            "handshake.accepted",
            "job.start",
            "shutdown",
        ]

    asyncio.run(scenario())


def test_hanging_job_hits_hard_deadline_and_terminates_entire_fake_job() -> None:
    async def scenario() -> None:
        adapter = _FakeJobAdapter()
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(crash_budget=0),
        )
        await supervisor.start()
        with pytest.raises(WorkerError, match="worker_job_deadline"):
            await supervisor.run_job(
                job_id="job-hang",
                job_kind="hang",
                hard_deadline_seconds=0.03,
            )
        await asyncio.sleep(0.03)
        assert adapter.processes[0].terminated
        assert adapter.processes[0].active == 0
        assert supervisor.actual_state is WorkerActualState.quarantined
        await supervisor.stop()

    asyncio.run(scenario())


def test_heartbeat_loss_is_a_hard_fault_not_a_fake_timeout() -> None:
    async def scenario() -> None:
        adapter = _FakeJobAdapter(({"heartbeat": False, "role": "perception"},))
        supervisor = WorkerSupervisor(
            name="perception-worker",
            role="perception",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(crash_budget=0),
        )
        await supervisor.start()
        await asyncio.sleep(0.12)
        assert adapter.processes[0].terminated
        assert supervisor.actual_state is WorkerActualState.quarantined
        assert (await supervisor.snapshot()).last_error_code == "worker_crash_budget_exhausted"
        await supervisor.stop()

    asyncio.run(scenario())


def test_crash_loop_uses_budget_backoff_and_quarantine() -> None:
    async def scenario() -> None:
        adapter = _FakeJobAdapter(
            (
                {"crash_after_handshake": True},
                {"crash_after_handshake": True},
                {"crash_after_handshake": True},
            )
        )
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(crash_budget=2),
        )
        await supervisor.start()
        async with asyncio.timeout(0.5):
            while supervisor.actual_state is not WorkerActualState.quarantined:
                await asyncio.sleep(0.005)
        snapshot = await supervisor.snapshot()
        assert snapshot.crash_count == 3
        assert snapshot.restart_count == 2
        assert len(adapter.processes) == 3
        assert snapshot.last_error_code == "worker_crash_budget_exhausted"
        await supervisor.stop()

    asyncio.run(scenario())


def test_shutdown_cancels_jobs_before_grace_and_scavenges_after_process_zero() -> None:
    async def scenario() -> None:
        adapter = _FakeJobAdapter()
        observed: list[int] = []
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            temp_scavenger=lambda: observed.append(adapter.processes[0].active),
            config=_config(),
        )
        await supervisor.start()
        job = asyncio.create_task(supervisor.run_job(job_id="cancel-race", job_kind="hang"))
        await asyncio.sleep(0)
        report = await supervisor.stop()
        with pytest.raises(WorkerError):
            await job
        assert adapter.processes[0].cancelled_jobs == ["cancel-race"]
        assert observed == [0]
        assert report.soft_cancelled_jobs == 1

    asyncio.run(scenario())


def test_oversized_stderr_is_drained_but_body_is_never_retained() -> None:
    async def scenario() -> None:
        stderr = (_SENTINEL.encode() + b"X" * 8192) * 256
        adapter = _FakeJobAdapter(({"stderr": stderr},))
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(),
        )
        await supervisor.start()
        async with asyncio.timeout(0.5):
            while (await supervisor.snapshot()).stderr_total_bytes < len(stderr):
                await asyncio.sleep(0.005)
        snapshot = await supervisor.snapshot()
        assert snapshot.stderr_total_bytes == len(stderr)
        assert snapshot.stderr_accounted_bytes == MAX_STDERR_BYTES
        assert snapshot.stderr_truncated
        serialized = json.dumps(asdict(snapshot)) + repr(supervisor.events)
        assert _SENTINEL not in serialized
        assert "private" not in serialized
        await supervisor.stop()

    asyncio.run(scenario())


def test_malformed_frame_after_handshake_terminates_and_quarantines() -> None:
    async def scenario() -> None:
        malformed = struct.pack(">I", 2) + b"{}"
        adapter = _FakeJobAdapter(({"malformed_after_handshake": malformed},))
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(crash_budget=0),
        )
        await supervisor.start()
        async with asyncio.timeout(0.3):
            while supervisor.actual_state is not WorkerActualState.quarantined:
                await asyncio.sleep(0.005)
        assert adapter.processes[0].terminated
        await supervisor.stop()

    asyncio.run(scenario())


def test_non_windows_adapter_is_explicitly_unsupported_and_never_claims_job_object() -> None:
    async def scenario() -> None:
        adapter = UnsupportedProcessAdapter()
        assert not adapter.supports_job_objects
        with pytest.raises(ProcessAdapterError, match="platform_unsupported"):
            await adapter.spawn(("anything",))
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(),
        )
        with pytest.raises(WorkerError, match="platform_unsupported"):
            await supervisor.start()
        assert supervisor.actual_state is WorkerActualState.unsupported
        health = await supervisor.check_health()
        assert health.error_code == "worker_platform_unsupported"
        await supervisor.stop()

    asyncio.run(scenario())


def test_capacity_duplicate_and_cancellation_races_remain_bounded() -> None:
    async def scenario() -> None:
        adapter = _FakeJobAdapter()
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(maximum_active_jobs=1),
        )
        await supervisor.start()
        first = asyncio.create_task(supervisor.run_job(job_id="one", job_kind="hang"))
        await asyncio.sleep(0)
        with pytest.raises(WorkerError, match="capacity_or_duplicate"):
            await supervisor.run_job(job_id="two", job_kind="hang")
        first.cancel()
        with suppress(asyncio.CancelledError):
            await first
        assert (await supervisor.snapshot()).active_jobs == 0
        await supervisor.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "overrides",
    (
        {"handshake_timeout_seconds": 0},
        {"restart_backoff_initial_seconds": 2, "restart_backoff_max_seconds": 1},
        {"maximum_active_jobs": 0},
        {"crash_budget": -1},
        {"stderr_limit_bytes": MAX_STDERR_BYTES + 1},
    ),
)
def test_supervisor_configuration_rejects_unbounded_or_invalid_values(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _config(**overrides)


def test_identity_empty_command_and_job_validation_fail_before_process_use() -> None:
    adapter = _FakeJobAdapter()
    with pytest.raises(ValueError, match="identity"):
        WorkerSupervisor(
            name="INVALID NAME",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
        )
    with pytest.raises(ValueError, match="empty"):
        WorkerSupervisor(name="media", role="media", command=(), adapter=adapter)

    async def scenario() -> None:
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(),
        )
        assert (await supervisor.check_health()).status.value == "disabled"
        with pytest.raises(WorkerError, match="identity_invalid"):
            await supervisor.run_job(job_id="bad id", job_kind="complete")
        with pytest.raises(WorkerError, match="not_accepting"):
            await supervisor.run_job(job_id="valid", job_kind="complete")
        await supervisor.start()
        await supervisor.start()
        assert (await supervisor.check_health()).status.value == "ready"
        with pytest.raises(WorkerError, match="deadline_invalid"):
            await supervisor.run_job(
                job_id="deadline",
                job_kind="complete",
                hard_deadline_seconds=999,
            )
        first = await supervisor.stop()
        second = await supervisor.stop()
        assert supervisor.last_shutdown_report == second
        assert first.active_processes == second.active_processes == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "factory,error_code",
    (
        ({"send_hello": False}, "worker_handshake_timeout"),
        ({"create_no_window": False}, "worker_no_window_not_enforced"),
    ),
)
def test_startup_failures_terminate_and_report_stable_codes(
    factory: dict[str, Any],
    error_code: str,
) -> None:
    async def scenario() -> None:
        adapter = _FakeJobAdapter((factory,))
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(),
        )
        with pytest.raises(WorkerError, match=error_code):
            await supervisor.start()
        assert supervisor.actual_state is WorkerActualState.failed
        assert (await supervisor.check_health()).error_code == error_code
        await supervisor.stop()

    asyncio.run(scenario())


def test_adapter_spawn_failure_is_stable_and_has_no_process_to_leak() -> None:
    async def scenario() -> None:
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=_FakeJobAdapter(spawn_error=True),
            config=_config(),
        )
        with pytest.raises(WorkerError, match="synthetic_spawn_failed"):
            await supervisor.start()
        snapshot = await supervisor.snapshot()
        assert snapshot.active_processes == 0
        await supervisor.stop()

    asyncio.run(scenario())


def test_shutdown_escalates_after_grace_and_reports_scavenge_and_query_failures() -> None:
    async def scenario() -> None:
        adapter = _FakeJobAdapter(
            ({"ignore_shutdown": True, "fail_query": True, "fail_close": True},)
        )

        def fail_scavenge() -> None:
            raise OSError(_SENTINEL)

        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            temp_scavenger=fail_scavenge,
            config=_config(),
        )
        await supervisor.start()
        assert (await supervisor.snapshot()).active_processes is None
        report = await supervisor.stop()
        assert report.hard_terminated
        assert report.active_processes is None
        assert not report.process_close_succeeded
        assert not report.scavenge_succeeded
        assert _SENTINEL not in repr(report)

    asyncio.run(scenario())


def test_write_failure_cancel_of_unknown_job_and_quarantined_restart_are_bounded() -> None:
    async def scenario() -> None:
        adapter = _FakeJobAdapter(({"fail_write": True},))
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=adapter,
            config=_config(),
        )
        with pytest.raises(WorkerError, match="pipe_write_failed"):
            await supervisor.start()
        await supervisor.cancel_job("missing")
        await supervisor.stop()

        crashing = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=_FakeJobAdapter(({"crash_after_handshake": True},)),
            config=_config(crash_budget=0),
        )
        await crashing.start()
        async with asyncio.timeout(0.3):
            while crashing.actual_state is not WorkerActualState.quarantined:
                await asyncio.sleep(0.005)
        with pytest.raises(WorkerError, match="quarantined"):
            await crashing.start()
        await crashing.stop()

    asyncio.run(scenario())


def test_message_specific_schema_rejects_replayed_heartbeat_and_hidden_fields() -> None:
    async def scenario() -> None:
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=("trusted-helper",),
            adapter=_FakeJobAdapter(({"heartbeat": False},)),
            config=_config(),
        )
        await supervisor.start()
        generation = supervisor._generation
        await supervisor._handle_message(
            HelperMessage(message_type="heartbeat", payload={"sequence": 1}),
            generation,
        )
        invalid = (
            HelperMessage(message_type="heartbeat", payload={"sequence": 1}),
            HelperMessage(message_type="heartbeat", payload={"sequence": True}),
            HelperMessage(message_type="stopped", payload={"hidden": "value"}),
            HelperMessage(
                message_type="job.cancelled",
                request_id="unknown",
                payload={"hidden": "value"},
            ),
            HelperMessage(
                message_type="job.failed",
                request_id="unknown",
                payload={"error_code": "unsafe code"},
            ),
        )
        for message in invalid:
            with pytest.raises(ProtocolError):
                await supervisor._handle_message(message, generation)
        await supervisor.stop()

    asyncio.run(scenario())
