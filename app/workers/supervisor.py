"""Reusable worker lifecycle owner with hard deadlines and bounded diagnostics."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from app.health import CapabilityCheck, CapabilityState
from app.workers.access import ApprovedResourcePolicy, ResourceReference
from app.workers.process import ManagedProcess, ProcessAdapter, ProcessAdapterError
from app.workers.protocol import FrameDecoder, HelperMessage, ProtocolError, encode_message

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
    def __call__(self) -> object: ...


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
        if any(value <= 0 for value in positive):
            raise ValueError("worker supervisor deadlines must be positive")
        if self.restart_backoff_max_seconds < self.restart_backoff_initial_seconds:
            raise ValueError("worker supervisor backoff bounds invalid")
        if self.maximum_active_jobs < 1 or self.maximum_active_jobs > 64:
            raise ValueError("worker supervisor active job bound invalid")
        if self.crash_budget < 0 or self.crash_budget > 32:
            raise ValueError("worker supervisor crash budget invalid")
        if self.stderr_limit_bytes < 1 or self.stderr_limit_bytes > MAX_STDERR_BYTES:
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
        self._start_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
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
        async with self._start_lock:
            if self._state is WorkerActualState.enabled:
                return
            if self._state is WorkerActualState.quarantined:
                raise WorkerError("worker_quarantined")
            self._desired_enabled = True
            self._deliberate_stop = False
            if not self._adapter.supports_job_objects:
                self._state = WorkerActualState.unsupported
                self._last_error_code = "worker_platform_unsupported"
                self._emit("worker.unsupported")
                raise WorkerError("worker_platform_unsupported")
            await self._spawn_and_handshake()

    async def _spawn_and_handshake(self) -> None:
        self._state = WorkerActualState.starting
        self._accepting_jobs = False
        self._last_error_code = None
        self._hello = asyncio.Event()
        self._heartbeat_sequence = 0
        self._decoder = FrameDecoder()
        self._generation += 1
        generation = self._generation
        self._emit("worker.starting")
        try:
            process = await self._adapter.spawn(
                self._command,
                inherited_handles=self._resource_policy.inherited_handle_values,
            )
            if not process.create_no_window:
                await process.close()
                raise WorkerError("worker_no_window_not_enforced")
            self._process = process
            self._last_heartbeat = self._clock()
            self._spawn_task(self._read_stdout(generation), "worker-stdout")
            self._spawn_task(self._read_stderr(generation), "worker-stderr")
            self._process_wait_task = self._spawn_task(
                self._watch_process(process, generation),
                "worker-process-wait",
            )
            async with asyncio.timeout(self._config.handshake_timeout_seconds):
                await self._hello.wait()
            await self._send(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": self._role},
                )
            )
        except TimeoutError as exc:
            await self._terminate_current("worker_handshake_timeout")
            raise WorkerError("worker_handshake_timeout") from exc
        except (ProcessAdapterError, ProtocolError, WorkerError, OSError) as exc:
            code = getattr(exc, "code", "worker_start_failed")
            await self._terminate_current(code)
            raise WorkerError(code) from exc
        self._state = WorkerActualState.enabled
        self._accepting_jobs = True
        self._emit("worker.enabled")
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

    async def _send(self, message: HelperMessage) -> None:
        process = self._process
        if process is None:
            raise WorkerError("worker_not_running")
        frame = encode_message(message)
        async with self._send_lock:
            try:
                await process.write_stdin(frame)
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
        deadline = hard_deadline_seconds or self._config.maximum_job_seconds
        if deadline <= 0 or deadline > self._config.maximum_job_seconds:
            raise WorkerError("worker_job_deadline_invalid")
        wire_resources = [self._resource_policy.wire_reference(item) for item in resources]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._jobs[job_id] = future
        try:
            await self._send(
                HelperMessage(
                    message_type="job.start",
                    request_id=job_id,
                    payload={"job_kind": job_kind, "resources": wire_resources},
                )
            )
            try:
                async with asyncio.timeout(deadline):
                    return await asyncio.shield(future)
            except TimeoutError as exc:
                await self._hard_fault("worker_job_deadline")
                raise WorkerError("worker_job_deadline") from exc
        except asyncio.CancelledError:
            await self.cancel_job(job_id)
            raise
        finally:
            self._jobs.pop(job_id, None)

    async def cancel_job(self, job_id: str) -> None:
        future = self._jobs.get(job_id)
        if future is None or future.done():
            return
        try:
            await self._send(
                HelperMessage(message_type="job.cancel", request_id=job_id, payload={})
            )
        except WorkerError:
            return

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

    async def _hard_fault(self, code: str) -> None:
        if self._deliberate_stop or self._state in {
            WorkerActualState.stopping,
            WorkerActualState.disabled,
            WorkerActualState.quarantined,
        }:
            return
        self._accepting_jobs = False
        self._last_error_code = code
        self._state = WorkerActualState.failed
        self._emit("worker.hard_fault")
        process = self._process
        if process is not None:
            with suppress(Exception):
                await process.terminate_tree()

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
        await self._scavenge()
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
        if self._desired_enabled and not self._deliberate_stop:
            self._restart_count += 1
            try:
                await self._spawn_and_handshake()
            except WorkerError:
                await self._handle_crash("worker_restart_failed")

    async def stop(self) -> ShutdownReport:
        async with self._stop_lock:
            if self._state is WorkerActualState.disabled and self._process is None:
                report = ShutdownReport(0, False, 0, None, True, True, True)
                self._last_report = report
                return report
            started = self._clock()
            close_budget = (
                self._config.soft_cancel_grace_seconds + self._config.terminate_wait_seconds
            )
            self._desired_enabled = False
            self._accepting_jobs = False
            self._deliberate_stop = True
            self._state = WorkerActualState.stopping
            self._emit("worker.stopping")
            jobs = tuple(self._jobs)
            for job_id in jobs:
                await self.cancel_job(job_id)
            with suppress(WorkerError):
                await self._send(HelperMessage(message_type="shutdown", payload={}))
            hard_terminated = False
            exit_code: int | None = None
            process = self._process
            if process is not None:
                wait_task = self._process_wait_task
                try:
                    if wait_task is not None:
                        async with asyncio.timeout(self._config.soft_cancel_grace_seconds):
                            exit_code = await asyncio.shield(wait_task)
                except TimeoutError:
                    hard_terminated = True
                    with suppress(Exception):
                        await process.terminate_tree()
                    if wait_task is not None:
                        with suppress(Exception):
                            async with asyncio.timeout(self._config.terminate_wait_seconds):
                                exit_code = await asyncio.shield(wait_task)
            active_processes: int | None = None if process is not None else 0
            if process is not None:
                with suppress(Exception):
                    active_processes = await process.active_process_count()
            process_close_succeeded = await self._close_current_process()
            scavenge_succeeded = await self._scavenge()
            for future in tuple(self._jobs.values()):
                if not future.done():
                    future.set_exception(WorkerError("worker_stopped"))
            self._jobs.clear()
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
                deadline_met=(self._clock() - started) <= close_budget + 0.25,
            )
            self._last_report = report
            return report

    async def _terminate_current(self, code: str) -> None:
        self._last_error_code = code
        self._state = WorkerActualState.failed
        # Invalidate readers/watchers before forcing an incomplete startup
        # down; otherwise the normal crash-restart path races this failure.
        self._generation += 1
        process = self._process
        if process is not None:
            with suppress(Exception):
                await process.terminate_tree()
            wait_task = self._process_wait_task
            if wait_task is not None:
                with suppress(Exception):
                    async with asyncio.timeout(self._config.terminate_wait_seconds):
                        await asyncio.shield(wait_task)
        await self._close_current_process()
        await self._scavenge()

    async def _close_current_process(self) -> bool:
        process = self._process
        self._process = None
        self._generation += 1
        current = asyncio.current_task()
        cancelled: list[asyncio.Task[Any]] = []
        for task in tuple(self._tasks):
            if task is not current and not task.done():
                task.cancel()
                cancelled.append(task)
        close_succeeded = True
        if process is not None:
            try:
                await process.close()
            except Exception:
                close_succeeded = False
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        self._process_wait_task = None
        return close_succeeded

    async def _scavenge(self) -> bool:
        if self._temp_scavenger is None:
            return True
        try:
            await asyncio.to_thread(self._temp_scavenger)
        except Exception:
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
