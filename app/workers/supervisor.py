"""Reusable worker lifecycle owner with hard deadlines and bounded diagnostics."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from app.health import CapabilityCheck, CapabilityState
from app.workers.access import ApprovedResourcePolicy, ResourceReference
from app.workers.process import ManagedProcess, ProcessAdapter, ProcessAdapterError
from app.workers.protocol import (
    MAX_PAYLOAD_KEYS,
    FrameDecoder,
    HelperMessage,
    ProtocolError,
    encode_message,
)

MAX_STDERR_BYTES: Final = 1024 * 1024
MAX_SUPERVISOR_EVENTS: Final = 256
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")


class WorkerActualState(StrEnum):
    disabled = "disabled"
    starting = "starting"
    enabled = "enabled"
    stopping = "stopping"
    failed = "failed"
    quarantined = "quarantined"
    unsupported = "unsupported"


class WorkerError(RuntimeError):
    """Stable worker failure without stderr, paths, payloads, or exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TempScavenger(Protocol):
    def __call__(self) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    handshake_timeout_seconds: float = 5.0
    heartbeat_timeout_seconds: float = 5.0
    soft_cancel_grace_seconds: float = 0.5
    terminate_wait_seconds: float = 2.0
    maximum_job_seconds: float = 120.0
    maximum_active_jobs: int = 8
    crash_budget: int = 3
    crash_window_seconds: float = 60.0
    restart_backoff_initial_seconds: float = 0.05
    restart_backoff_max_seconds: float = 2.0
    stderr_limit_bytes: int = MAX_STDERR_BYTES

    def __post_init__(self) -> None:
        positive = (
            self.handshake_timeout_seconds,
            self.heartbeat_timeout_seconds,
            self.soft_cancel_grace_seconds,
            self.terminate_wait_seconds,
            self.maximum_job_seconds,
            self.crash_window_seconds,
            self.restart_backoff_initial_seconds,
            self.restart_backoff_max_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in positive
        ):
            raise ValueError("worker supervisor deadlines must be positive")
        if self.restart_backoff_max_seconds < self.restart_backoff_initial_seconds:
            raise ValueError("worker supervisor backoff bounds invalid")
        if (
            isinstance(self.maximum_active_jobs, bool)
            or not isinstance(self.maximum_active_jobs, int)
            or self.maximum_active_jobs < 1
            or self.maximum_active_jobs > 64
        ):
            raise ValueError("worker supervisor active job bound invalid")
        if (
            isinstance(self.crash_budget, bool)
            or not isinstance(self.crash_budget, int)
            or self.crash_budget < 0
            or self.crash_budget > 32
        ):
            raise ValueError("worker supervisor crash budget invalid")
        if (
            isinstance(self.stderr_limit_bytes, bool)
            or not isinstance(self.stderr_limit_bytes, int)
            or self.stderr_limit_bytes < 1
            or self.stderr_limit_bytes > MAX_STDERR_BYTES
        ):
            raise ValueError("worker supervisor stderr bound invalid")


@dataclass(frozen=True, slots=True)
class SupervisorEvent:
    code: str
    state: WorkerActualState
    monotonic_seconds: float
    count: int = 0


@dataclass(frozen=True, slots=True)
class SupervisorSnapshot:
    actual_state: WorkerActualState
    desired_enabled: bool
    accepting_jobs: bool
    active_jobs: int
    crash_count: int
    restart_count: int
    stderr_total_bytes: int
    stderr_accounted_bytes: int
    stderr_truncated: bool
    active_processes: int | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    soft_cancelled_jobs: int
    hard_terminated: bool
    active_processes: int | None
    exit_code: int | None
    process_close_succeeded: bool
    scavenge_succeeded: bool
    deadline_met: bool


class WorkerSupervisor:
    """The sole owner of one helper process, its Job, pipes, and job futures."""

    required_for_readiness = False

    def __init__(
        self,
        *,
        name: str,
        role: str,
        command: Sequence[str],
        adapter: ProcessAdapter,
        resource_policy: ApprovedResourcePolicy | None = None,
        temp_scavenger: TempScavenger | None = None,
        config: SupervisorConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _SAFE_CODE.fullmatch(name) or not _SAFE_CODE.fullmatch(role):
            raise ValueError("worker supervisor identity invalid")
        selected_command = tuple(command)
        if not selected_command:
            raise ValueError("worker supervisor command is empty")
        self.name = name
        self._role = role
        self._command = selected_command
        self._adapter = adapter
        self._resource_policy = resource_policy or ApprovedResourcePolicy()
        if temp_scavenger is not None and not _is_async_callable(temp_scavenger):
            raise ValueError("worker temp scavenger must be async")
        self._temp_scavenger = temp_scavenger
        self._config = config or SupervisorConfig()
        self._clock = clock
        self._state = WorkerActualState.disabled
        self._desired_enabled = False
        self._accepting_jobs = False
        self._process: ManagedProcess | None = None
        self._decoder = FrameDecoder()
        self._hello = asyncio.Event()
        self._last_heartbeat = 0.0
        self._heartbeat_sequence = 0
        self._jobs: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._process_wait_task: asyncio.Task[int] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_epoch = 0
        self._start_operation: asyncio.Task[None] | None = None
        self._stop_operation: asyncio.Task[ShutdownReport] | None = None
        self._send_lock = asyncio.Lock()
        self._crashes: deque[float] = deque(maxlen=33)
        self._restart_count = 0
        self._stderr_total = 0
        self._stderr_accounted = 0
        self._stderr_truncated = False
        self._last_error_code: str | None = None
        self._events: deque[SupervisorEvent] = deque(maxlen=MAX_SUPERVISOR_EVENTS)
        self._generation = 0
        self._deliberate_stop = False
        self._last_report: ShutdownReport | None = None

    @property
    def actual_state(self) -> WorkerActualState:
        return self._state

    @property
    def events(self) -> tuple[SupervisorEvent, ...]:
        return tuple(self._events)

    @property
    def last_shutdown_report(self) -> ShutdownReport | None:
        return self._last_report

    async def check_health(self) -> CapabilityCheck:
        if self._state is WorkerActualState.enabled:
            return CapabilityCheck(status=CapabilityState.ready)
        if self._state is WorkerActualState.disabled:
            return CapabilityCheck(status=CapabilityState.disabled)
        if self._state in {WorkerActualState.starting, WorkerActualState.stopping}:
            return CapabilityCheck(
                status=CapabilityState.degraded,
                error_code="worker_transitioning",
            )
        code = self._last_error_code or "worker_unavailable"
        return CapabilityCheck(status=CapabilityState.unavailable, error_code=code)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._state is WorkerActualState.enabled:
                return
            if self._state is WorkerActualState.quarantined:
                raise WorkerError("worker_quarantined")
            if self._state is WorkerActualState.stopping:
                raise WorkerError("worker_stopping")
            if not self._adapter.supports_job_objects:
                self._desired_enabled = True
                self._state = WorkerActualState.unsupported
                self._last_error_code = "worker_platform_unsupported"
                self._emit("worker.unsupported")
                raise WorkerError("worker_platform_unsupported")
            existing = self._start_operation
            if existing is not None and not existing.done():
                operation = existing
            else:
                self._lifecycle_epoch += 1
                epoch = self._lifecycle_epoch
                self._state = WorkerActualState.starting
                self._accepting_jobs = False
                self._last_error_code = None
                self._hello = asyncio.Event()
                self._heartbeat_sequence = 0
                self._decoder = FrameDecoder()
                self._emit("worker.starting")
                operation = asyncio.create_task(
                    self._spawn_and_handshake(epoch),
                    name=f"worker-start-{self.name}-{epoch}",
                )
                self._start_operation = operation
            self._desired_enabled = True
            self._deliberate_stop = False
        try:
            await asyncio.shield(operation)
        finally:
            async with self._lifecycle_lock:
                if self._start_operation is operation and operation.done():
                    self._start_operation = None

    async def _spawn_and_handshake(self, epoch: int) -> None:
        process: ManagedProcess | None = None
        try:
            process = await self._adapter.spawn(
                self._command,
                inherited_handles=self._resource_policy.inherited_handle_values,
            )
            if not process.create_no_window:
                await process.close()
                process = None
                raise WorkerError("worker_no_window_not_enforced")
            async with self._lifecycle_lock:
                if (
                    epoch != self._lifecycle_epoch
                    or not self._desired_enabled
                    or self._state is not WorkerActualState.starting
                ):
                    stale = True
                else:
                    stale = False
                    self._generation += 1
                    generation = self._generation
                    self._process = process
            if stale:
                await process.close()
                process = None
                raise WorkerError("worker_start_cancelled")
            self._last_heartbeat = self._clock()
            self._spawn_task(self._read_stdout(generation), "worker-stdout")
            self._spawn_task(self._read_stderr(generation), "worker-stderr")
            self._process_wait_task = self._spawn_task(
                self._watch_process(process, generation),
                "worker-process-wait",
            )
            handshake_deadline = (
                asyncio.get_running_loop().time() + self._config.handshake_timeout_seconds
            )
            async with asyncio.timeout_at(handshake_deadline):
                await self._hello.wait()
            await self._send(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={
                        "role": self._role,
                        "maximum_active_jobs": self._config.maximum_active_jobs,
                    },
                ),
                deadline_at=handshake_deadline,
            )
        except TimeoutError as exc:
            if epoch != self._lifecycle_epoch:
                raise WorkerError("worker_start_cancelled") from exc
            await self._terminate_current("worker_handshake_timeout", epoch=epoch)
            raise WorkerError("worker_handshake_timeout") from exc
        except (ProcessAdapterError, ProtocolError, WorkerError, OSError) as exc:
            code = getattr(exc, "code", "worker_start_failed")
            if epoch != self._lifecycle_epoch:
                if process is not None and process is not self._process:
                    with suppress(Exception):
                        await process.close()
                raise WorkerError("worker_start_cancelled") from exc
            await self._terminate_current(code, epoch=epoch)
            raise WorkerError(code) from exc
        async with self._lifecycle_lock:
            if epoch != self._lifecycle_epoch or not self._desired_enabled:
                stale = True
            else:
                stale = False
                self._state = WorkerActualState.enabled
                self._accepting_jobs = True
                self._emit("worker.enabled")
        if stale:
            await self._close_current_process(expected=process)
            raise WorkerError("worker_start_cancelled")
        self._spawn_task(self._monitor_heartbeat(generation), "worker-heartbeat")

    def _spawn_task(self, coroutine: Any, label: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=f"{label}-{self.name}-{self._generation}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _read_stdout(self, generation: int) -> None:
        process = self._process
        if process is None:
            return
        try:
            while generation == self._generation:
                chunk = await process.read_stdout(8192)
                if not chunk:
                    self._decoder.finish()
                    return
                for message in self._decoder.feed(chunk):
                    await self._handle_message(message, generation)
        except (ProtocolError, OSError, ProcessAdapterError) as exc:
            if generation == self._generation and not self._deliberate_stop:
                await self._hard_fault(getattr(exc, "code", "worker_protocol_failed"))

    async def _read_stderr(self, generation: int) -> None:
        process = self._process
        if process is None:
            return
        try:
            while generation == self._generation:
                chunk = await process.read_stderr(8192)
                if not chunk:
                    return
                self._stderr_total += len(chunk)
                remaining = max(0, self._config.stderr_limit_bytes - self._stderr_accounted)
                self._stderr_accounted += min(remaining, len(chunk))
                self._stderr_truncated = self._stderr_truncated or len(chunk) > remaining
        except (OSError, ProcessAdapterError):
            return

    async def _handle_message(self, message: HelperMessage, generation: int) -> None:
        if generation != self._generation:
            return
        if message.message_type == "hello":
            if (
                self._hello.is_set()
                or message.request_id is not None
                or message.payload != {"role": self._role}
            ):
                raise ProtocolError("helper_protocol_handshake_invalid")
            self._last_heartbeat = self._clock()
            self._hello.set()
            return
        if not self._hello.is_set():
            raise ProtocolError("helper_protocol_handshake_required")
        if message.message_type == "heartbeat":
            sequence = message.payload.get("sequence")
            if (
                message.request_id is not None
                or set(message.payload) != {"sequence"}
                or isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence <= self._heartbeat_sequence
            ):
                raise ProtocolError("helper_protocol_heartbeat_invalid")
            self._heartbeat_sequence = sequence
            self._last_heartbeat = self._clock()
            return
        if message.message_type in {"job.completed", "job.cancelled", "job.failed"}:
            request_id = message.request_id
            if request_id is None:
                raise ProtocolError("helper_protocol_job_id_missing")
            if message.message_type == "job.cancelled" and message.payload:
                raise ProtocolError("helper_protocol_job_result_invalid")
            if message.message_type == "job.failed":
                error_code = message.payload.get("error_code")
                if (
                    set(message.payload) != {"error_code"}
                    or not isinstance(error_code, str)
                    or not _SAFE_CODE.fullmatch(error_code)
                ):
                    raise ProtocolError("helper_protocol_job_result_invalid")
            future = self._jobs.pop(request_id, None)
            if future is None:
                return
            if future.done():
                return
            if message.message_type == "job.completed":
                future.set_result(message.payload)
            elif message.message_type == "job.cancelled":
                future.set_exception(WorkerError("worker_job_cancelled"))
            else:
                future.set_exception(WorkerError("worker_job_failed"))
            return
        if message.message_type == "stopped":
            if message.request_id is not None or message.payload:
                raise ProtocolError("helper_protocol_stopped_invalid")
            return
        raise ProtocolError("helper_protocol_message_unexpected")

    async def _send(self, message: HelperMessage, *, deadline_at: float) -> None:
        process = self._process
        if process is None:
            raise WorkerError("worker_not_running")
        frame = encode_message(message)
        try:
            async with asyncio.timeout_at(deadline_at):
                async with self._send_lock:
                    await process.write_stdin(frame)
        except TimeoutError:
            raise
        except (OSError, ProcessAdapterError) as exc:
            raise WorkerError("worker_pipe_write_failed") from exc

    async def run_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        resources: Sequence[ResourceReference] = (),
        hard_deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not _SAFE_ID.fullmatch(job_id) or not _SAFE_CODE.fullmatch(job_kind):
            raise WorkerError("worker_job_identity_invalid")
        if not self._accepting_jobs or self._state is not WorkerActualState.enabled:
            raise WorkerError("worker_not_accepting_jobs")
        if job_id in self._jobs or len(self._jobs) >= self._config.maximum_active_jobs:
            raise WorkerError("worker_job_capacity_or_duplicate")
        if hard_deadline_seconds is None:
            deadline = self._config.maximum_job_seconds
        elif (
            isinstance(hard_deadline_seconds, bool)
            or not isinstance(hard_deadline_seconds, (int, float))
            or not math.isfinite(hard_deadline_seconds)
        ):
            raise WorkerError("worker_job_deadline_invalid")
        else:
            deadline = float(hard_deadline_seconds)
        if deadline <= 0 or deadline > self._config.maximum_job_seconds:
            raise WorkerError("worker_job_deadline_invalid")
        if len(resources) > MAX_PAYLOAD_KEYS:
            raise WorkerError("worker_job_resources_invalid")
        wire_resources = [self._resource_policy.wire_reference(item) for item in resources]
        try:
            start_message = HelperMessage(
                message_type="job.start",
                request_id=job_id,
                payload={"job_kind": job_kind, "resources": wire_resources},
            )
            # Preflight the complete frame before reserving a job slot.  The
            # subsequent send re-encodes the same private, unchanged shape.
            encode_message(start_message)
        except ProtocolError as exc:
            raise WorkerError("worker_job_protocol_invalid") from exc
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + deadline
        termination_reserve = min(self._config.terminate_wait_seconds, deadline / 2)
        work_deadline = deadline_at - termination_reserve
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._jobs[job_id] = future
        try:
            try:
                async with asyncio.timeout_at(work_deadline):
                    await self._send(
                        start_message,
                        deadline_at=work_deadline,
                    )
                    return await asyncio.shield(future)
            except TimeoutError as exc:
                await self._hard_fault("worker_job_deadline", deadline_at=deadline_at)
                raise WorkerError("worker_job_deadline") from exc
            except WorkerError as exc:
                if exc.code == "worker_pipe_write_failed":
                    await self._hard_fault(exc.code, deadline_at=deadline_at)
                raise
        except asyncio.CancelledError:
            await self._settle_caller_cancel(job_id, future, deadline_at)
            raise

    async def cancel_job(self, job_id: str, *, deadline_at: float | None = None) -> None:
        future = self._jobs.get(job_id)
        if future is None or future.done():
            return
        if deadline_at is None:
            deadline_at = asyncio.get_running_loop().time() + self._config.soft_cancel_grace_seconds
        try:
            await self._send(
                HelperMessage(message_type="job.cancel", request_id=job_id, payload={}),
                deadline_at=deadline_at,
            )
        except (TimeoutError, WorkerError):
            return

    async def _settle_caller_cancel(
        self,
        job_id: str,
        future: asyncio.Future[dict[str, Any]],
        deadline_at: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        remaining = max(0.0, deadline_at - loop.time())
        terminate_reserve = min(self._config.terminate_wait_seconds, remaining / 2)
        soft_deadline = min(
            loop.time() + self._config.soft_cancel_grace_seconds,
            deadline_at - terminate_reserve,
        )
        await self.cancel_job(job_id, deadline_at=soft_deadline)
        settled = future.done()
        if not settled and soft_deadline > loop.time():
            try:
                async with asyncio.timeout_at(soft_deadline):
                    await asyncio.shield(future)
            except (TimeoutError, WorkerError):
                pass
            settled = future.done()
        if settled:
            self._jobs.pop(job_id, None)
            return
        tree_zero = await self._hard_fault(
            "worker_job_cancel_unsettled",
            deadline_at=deadline_at,
        )
        if tree_zero:
            self._jobs.pop(job_id, None)

    async def _monitor_heartbeat(self, generation: int) -> None:
        while generation == self._generation and self._state is WorkerActualState.enabled:
            remaining = self._config.heartbeat_timeout_seconds - (
                self._clock() - self._last_heartbeat
            )
            if remaining <= 0:
                await self._hard_fault("worker_heartbeat_lost")
                return
            await asyncio.sleep(min(remaining, 0.1))

    async def _watch_process(
        self,
        process: ManagedProcess,
        generation: int,
    ) -> int:
        try:
            code = await process.wait()
        except (OSError, ProcessAdapterError):
            code = 1
        if generation != self._generation or self._deliberate_stop:
            return code
        await self._handle_crash("worker_process_exited")
        return code

    async def _hard_fault(self, code: str, *, deadline_at: float | None = None) -> bool:
        if self._deliberate_stop or self._state in {
            WorkerActualState.stopping,
            WorkerActualState.disabled,
            WorkerActualState.quarantined,
        }:
            return False
        self._accepting_jobs = False
        self._last_error_code = code
        self._state = WorkerActualState.failed
        self._emit("worker.hard_fault")
        process = self._process
        if process is None:
            return True
        if deadline_at is None:
            deadline_at = asyncio.get_running_loop().time() + self._config.terminate_wait_seconds
        try:
            async with asyncio.timeout_at(deadline_at):
                await process.terminate_tree()
                while True:
                    active = await process.active_process_count()
                    if active == 0:
                        for future in tuple(self._jobs.values()):
                            if not future.done():
                                future.set_exception(WorkerError(code))
                                future.exception()
                        self._jobs.clear()
                        return True
                    await asyncio.sleep(0)
        except (TimeoutError, OSError, ProcessAdapterError):
            return False

    async def _handle_crash(self, code: str) -> None:
        self._accepting_jobs = False
        self._last_error_code = code
        now = self._clock()
        cutoff = now - self._config.crash_window_seconds
        while self._crashes and self._crashes[0] < cutoff:
            self._crashes.popleft()
        self._crashes.append(now)
        for future in tuple(self._jobs.values()):
            if not future.done():
                future.set_exception(WorkerError(code))
        self._jobs.clear()
        await self._close_current_process()
        await self._scavenge(
            asyncio.get_running_loop().time() + self._config.terminate_wait_seconds
        )
        if len(self._crashes) > self._config.crash_budget:
            self._state = WorkerActualState.quarantined
            self._desired_enabled = False
            self._last_error_code = "worker_crash_budget_exhausted"
            self._emit("worker.quarantined", len(self._crashes))
            return
        self._state = WorkerActualState.failed
        self._emit("worker.crashed", len(self._crashes))
        if not self._desired_enabled:
            return
        delay = min(
            self._config.restart_backoff_max_seconds,
            self._config.restart_backoff_initial_seconds * (2 ** max(0, len(self._crashes) - 1)),
        )
        await asyncio.sleep(delay)
        async with self._lifecycle_lock:
            if not self._desired_enabled or self._deliberate_stop:
                return
            self._restart_count += 1
            epoch = self._lifecycle_epoch
            self._state = WorkerActualState.starting
            self._hello = asyncio.Event()
            self._heartbeat_sequence = 0
            self._decoder = FrameDecoder()
            self._emit("worker.starting")
        try:
            await self._spawn_and_handshake(epoch)
        except WorkerError:
            await self._handle_crash("worker_restart_failed")

    async def stop(self) -> ShutdownReport:
        async with self._lifecycle_lock:
            existing = self._stop_operation
            if existing is not None and not existing.done():
                operation = existing
            elif (
                self._state is WorkerActualState.disabled
                and self._process is None
                and (self._start_operation is None or self._start_operation.done())
            ):
                report = ShutdownReport(0, False, 0, None, True, True, True)
                self._last_report = report
                return report
            else:
                self._lifecycle_epoch += 1
                epoch = self._lifecycle_epoch
                self._desired_enabled = False
                self._accepting_jobs = False
                self._deliberate_stop = True
                self._state = WorkerActualState.stopping
                self._emit("worker.stopping")
                operation = asyncio.create_task(
                    self._stop_impl(epoch),
                    name=f"worker-stop-{self.name}-{epoch}",
                )
                self._stop_operation = operation
        try:
            return await asyncio.shield(operation)
        finally:
            async with self._lifecycle_lock:
                if self._stop_operation is operation and operation.done():
                    self._stop_operation = None

    async def _stop_impl(self, epoch: int) -> ShutdownReport:
        loop = asyncio.get_running_loop()
        started = loop.time()
        close_budget = self._config.soft_cancel_grace_seconds + self._config.terminate_wait_seconds
        deadline_at = started + close_budget
        close_reserve = min(0.05, self._config.terminate_wait_seconds / 4)
        operation_deadline = deadline_at - close_reserve
        soft_deadline = min(deadline_at, started + self._config.soft_cancel_grace_seconds)
        jobs = tuple(self._jobs)
        for job_id in jobs:
            await self.cancel_job(job_id, deadline_at=soft_deadline)
            if loop.time() >= soft_deadline:
                break
        with suppress(TimeoutError, WorkerError):
            await self._send(
                HelperMessage(message_type="shutdown", payload={}),
                deadline_at=soft_deadline,
            )
        hard_terminated = False
        exit_code: int | None = None
        process = self._process
        wait_task = self._process_wait_task
        if process is not None and wait_task is not None:
            if wait_task.done():
                with suppress(OSError, ProcessAdapterError, asyncio.CancelledError):
                    exit_code = wait_task.result()
            elif loop.time() < soft_deadline:
                try:
                    async with asyncio.timeout_at(soft_deadline):
                        exit_code = await asyncio.shield(wait_task)
                except TimeoutError:
                    pass
        if process is not None and (wait_task is None or not wait_task.done()):
            hard_terminated = True
            try:
                async with asyncio.timeout_at(operation_deadline):
                    await process.terminate_tree()
            except (TimeoutError, OSError, ProcessAdapterError):
                pass
            if wait_task is not None and loop.time() < operation_deadline:
                try:
                    async with asyncio.timeout_at(operation_deadline):
                        exit_code = await asyncio.shield(wait_task)
                except TimeoutError:
                    pass
        active_processes: int | None = None if process is not None else 0
        if process is not None and loop.time() < operation_deadline:
            try:
                async with asyncio.timeout_at(operation_deadline):
                    active_processes = await process.active_process_count()
            except (TimeoutError, OSError, ProcessAdapterError):
                active_processes = None
        process_close_succeeded = False
        try:
            if loop.time() < deadline_at:
                async with asyncio.timeout_at(deadline_at):
                    process_close_succeeded = await self._close_current_process()
            else:
                process_close_succeeded = await self._close_current_process()
        except TimeoutError:
            process_close_succeeded = False
        if self._process is not None:
            # A missed deadline is reportable, but it must never discard the
            # only owner of a live Job/process handle.  Trusted adapters still
            # get one safety close after the timed phase has expired.
            process_close_succeeded = await self._close_current_process()
        scavenge_succeeded = await self._scavenge(deadline_at)
        for future in tuple(self._jobs.values()):
            if not future.done():
                future.set_exception(WorkerError("worker_stopped"))
        self._jobs.clear()
        async with self._lifecycle_lock:
            if epoch == self._lifecycle_epoch:
                self._state = WorkerActualState.disabled
                self._last_error_code = None
                self._emit("worker.disabled")
        report = ShutdownReport(
            soft_cancelled_jobs=len(jobs),
            hard_terminated=hard_terminated,
            active_processes=active_processes,
            exit_code=exit_code,
            process_close_succeeded=process_close_succeeded,
            scavenge_succeeded=scavenge_succeeded,
            deadline_met=loop.time() <= deadline_at,
        )
        self._last_report = report
        return report

    async def _terminate_current(self, code: str, *, epoch: int) -> None:
        if epoch != self._lifecycle_epoch:
            return
        self._last_error_code = code
        self._state = WorkerActualState.failed
        # Invalidate readers/watchers before forcing an incomplete startup
        # down; otherwise the normal crash-restart path races this failure.
        self._generation += 1
        process = self._process
        deadline_at = asyncio.get_running_loop().time() + self._config.terminate_wait_seconds
        if process is not None:
            try:
                async with asyncio.timeout_at(deadline_at):
                    await process.terminate_tree()
            except (TimeoutError, OSError, ProcessAdapterError):
                pass
            wait_task = self._process_wait_task
            if wait_task is not None and asyncio.get_running_loop().time() < deadline_at:
                try:
                    async with asyncio.timeout_at(deadline_at):
                        await asyncio.shield(wait_task)
                except (TimeoutError, OSError, ProcessAdapterError):
                    pass
        await self._close_current_process()
        await self._scavenge(deadline_at)

    async def _close_current_process(self, *, expected: ManagedProcess | None = None) -> bool:
        process = self._process
        if expected is not None and process is not expected:
            return True
        self._generation += 1
        current = asyncio.current_task()
        cancelled: list[asyncio.Task[Any]] = []
        for task in tuple(self._tasks):
            if task is not current and not task.done():
                task.cancel()
                cancelled.append(task)
        close_succeeded = True
        if process is not None:
            close_completed = False
            try:
                await process.close()
            except Exception:
                close_succeeded = False
                close_completed = True
            else:
                close_completed = True
            if close_completed and self._process is process:
                self._process = None
        else:
            self._process = None
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        self._process_wait_task = None
        return close_succeeded

    async def _scavenge(self, deadline_at: float) -> bool:
        if self._temp_scavenger is None:
            return True
        try:
            async with asyncio.timeout_at(deadline_at):
                await self._temp_scavenger()
        except (TimeoutError, Exception):
            self._last_error_code = "worker_temp_scavenge_failed"
            return False
        return True

    async def snapshot(self) -> SupervisorSnapshot:
        process = self._process
        active: int | None = 0
        if process is not None:
            try:
                active = await process.active_process_count()
            except Exception:
                active = None
        return SupervisorSnapshot(
            actual_state=self._state,
            desired_enabled=self._desired_enabled,
            accepting_jobs=self._accepting_jobs,
            active_jobs=len(self._jobs),
            crash_count=len(self._crashes),
            restart_count=self._restart_count,
            stderr_total_bytes=self._stderr_total,
            stderr_accounted_bytes=self._stderr_accounted,
            stderr_truncated=self._stderr_truncated,
            active_processes=active,
            last_error_code=self._last_error_code,
        )

    def _emit(self, code: str, count: int = 0) -> None:
        self._events.append(
            SupervisorEvent(
                code=code,
                state=self._state,
                monotonic_seconds=self._clock(),
                count=count,
            )
        )


def _is_async_callable(value: object) -> bool:
    return inspect.iscoroutinefunction(value) or (
        callable(value) and inspect.iscoroutinefunction(type(value).__call__)
    )
