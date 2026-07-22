"""Base runtime for future MediaWorker and PerceptionWorker entry points.

This module owns only handshake, heartbeat, cancellation, and bounded framing.
Concrete audio/STT/perception job handlers are intentionally absent in W12.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Protocol

from app.workers.access import ApprovedResourcePolicy, AuthorizedResource, ResourceAccessError
from app.workers.protocol import FrameDecoder, HelperMessage, ProtocolError, encode_message

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class HelperJobHandler(Protocol):
    async def run_job(
        self,
        job_kind: str,
        resources: tuple[AuthorizedResource, ...],
        cancelled: asyncio.Event,
        *,
        job_id: str,
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class HelperRuntime:
    """One anonymous-pipe helper runtime with no listening or named objects."""

    def __init__(
        self,
        *,
        role: str,
        handler: HelperJobHandler,
        resource_policy: ApprovedResourcePolicy | None = None,
        heartbeat_interval_seconds: float = 0.25,
        maximum_active_jobs: int = 8,
        read: Callable[[int], Awaitable[bytes]] | None = None,
        write: Callable[[bytes], Awaitable[None]] | None = None,
    ) -> None:
        if (
            not _SAFE_CODE.fullmatch(role)
            or isinstance(heartbeat_interval_seconds, bool)
            or not isinstance(heartbeat_interval_seconds, (int, float))
            or not math.isfinite(heartbeat_interval_seconds)
            or heartbeat_interval_seconds <= 0
            or isinstance(maximum_active_jobs, bool)
            or not isinstance(maximum_active_jobs, int)
            or not 1 <= maximum_active_jobs <= 64
        ):
            raise ValueError("helper runtime configuration invalid")
        self._role = role
        self._handler = handler
        self._resource_policy = resource_policy or ApprovedResourcePolicy()
        self._heartbeat_interval = heartbeat_interval_seconds
        self._maximum_active_jobs = maximum_active_jobs
        self._active_job_limit = maximum_active_jobs
        self._read = read or self._read_stdin
        self._write = write or self._write_stdout
        self._write_lock = asyncio.Lock()
        self._jobs: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}
        self._stopping = False
        self._handshake = asyncio.Event()

    async def run(self) -> int:
        try:
            await self._send(HelperMessage(message_type="hello", payload={"role": self._role}))
        except (OSError, ProtocolError):
            try:
                await self._handler.close()
            except Exception:
                return 3
            return 2
        heartbeat = asyncio.create_task(self._heartbeat(), name="helper-heartbeat")
        decoder = FrameDecoder()
        exit_code = 0
        try:
            while not self._stopping:
                chunk = await self._read(8192)
                if not chunk:
                    decoder.finish()
                    break
                for message in decoder.feed(chunk):
                    await self._dispatch(message)
        except (OSError, ProtocolError):
            exit_code = 2
        finally:
            self._stopping = True
            for task, cancelled in tuple(self._jobs.values()):
                cancelled.set()
                task.cancel()
            await asyncio.gather(*(item[0] for item in self._jobs.values()), return_exceptions=True)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await self._handler.close()
            except Exception:
                exit_code = 3
        if exit_code == 0:
            with suppress(OSError):
                await self._send(HelperMessage(message_type="stopped", payload={}))
        return exit_code

    async def _dispatch(self, message: HelperMessage) -> None:
        if not self._handshake.is_set():
            requested_limit = message.payload.get("maximum_active_jobs")
            if message.payload == {"role": self._role}:
                requested_limit = self._maximum_active_jobs
            if (
                message.message_type != "handshake.accepted"
                or message.request_id is not None
                or set(message.payload)
                not in (
                    {"role"},
                    {"role", "maximum_active_jobs"},
                )
                or message.payload.get("role") != self._role
                or isinstance(requested_limit, bool)
                or not isinstance(requested_limit, int)
                or not 1 <= requested_limit <= self._maximum_active_jobs
            ):
                raise ProtocolError("helper_protocol_handshake_invalid")
            self._active_job_limit = requested_limit
            self._handshake.set()
            return
        if message.message_type == "job.start":
            await self._start_job(message)
            return
        if message.message_type == "job.cancel":
            await self._cancel_job(message)
            return
        if (
            message.message_type == "shutdown"
            and message.request_id is None
            and not message.payload
        ):
            self._stopping = True
            for _task, cancelled in self._jobs.values():
                cancelled.set()
            return
        raise ProtocolError("helper_protocol_message_unexpected")

    async def _start_job(self, message: HelperMessage) -> None:
        job_id = message.request_id
        job_kind = message.payload.get("job_kind")
        resources = message.payload.get("resources")
        if (
            job_id is None
            or job_id in self._jobs
            or set(message.payload) != {"job_kind", "resources"}
            or not isinstance(job_kind, str)
            or not _SAFE_CODE.fullmatch(job_kind)
            or not isinstance(resources, list)
        ):
            raise ProtocolError("helper_protocol_job_invalid")
        if len(self._jobs) >= self._active_job_limit:
            await self._send(
                HelperMessage(
                    message_type="job.failed",
                    request_id=job_id,
                    payload={"error_code": "worker_job_capacity"},
                )
            )
            return
        cancelled = asyncio.Event()
        task = asyncio.create_task(
            self._run_job(job_id, job_kind, resources, cancelled),
            name=f"helper-job-{job_id}",
        )
        self._jobs[job_id] = (task, cancelled)

    async def _run_job(
        self,
        job_id: str,
        job_kind: str,
        resources: list[dict[str, Any]],
        cancelled: asyncio.Event,
    ) -> None:
        authorized_items: list[AuthorizedResource] = []
        try:
            try:
                for resource in resources:
                    authorized_items.append(self._resource_policy.authorize_wire(resource))
            except ResourceAccessError as exc:
                raise ProtocolError("helper_protocol_resource_rejected") from exc
            authorized = tuple(authorized_items)
            result = await self._handler.run_job(
                job_kind,
                authorized,
                cancelled,
                job_id=job_id,
            )
            if not isinstance(result, dict):
                raise ProtocolError("helper_protocol_job_result_invalid")
            await self._send(
                HelperMessage(
                    message_type="job.cancelled" if cancelled.is_set() else "job.completed",
                    request_id=job_id,
                    payload={} if cancelled.is_set() else result,
                )
            )
        except asyncio.CancelledError:
            with suppress(OSError):
                await self._send(
                    HelperMessage(message_type="job.cancelled", request_id=job_id, payload={})
                )
            raise
        except Exception:
            with suppress(OSError):
                await self._send(
                    HelperMessage(
                        message_type="job.failed",
                        request_id=job_id,
                        payload={"error_code": "worker_job_failed"},
                    )
                )
        finally:
            for authorized_resource in authorized_items:
                with suppress(OSError):
                    authorized_resource.close()
            self._jobs.pop(job_id, None)

    async def _cancel_job(self, message: HelperMessage) -> None:
        if message.request_id is None or message.payload:
            raise ProtocolError("helper_protocol_cancel_invalid")
        item = self._jobs.get(message.request_id)
        if item is not None:
            item[1].set()

    async def _heartbeat(self) -> None:
        sequence = 0
        while not self._stopping:
            await self._handshake.wait()
            await asyncio.sleep(self._heartbeat_interval)
            sequence += 1
            await self._send(
                HelperMessage(message_type="heartbeat", payload={"sequence": sequence})
            )

    async def _send(self, message: HelperMessage) -> None:
        frame = encode_message(message)
        async with self._write_lock:
            await self._write(frame)

    @staticmethod
    async def _read_stdin(size: int) -> bytes:
        return await asyncio.to_thread(os.read, sys.stdin.fileno(), size)

    @staticmethod
    async def _write_stdout(data: bytes) -> None:
        def write_all() -> None:
            view = memoryview(data)
            while view:
                written = os.write(sys.stdout.fileno(), view)
                if written <= 0:
                    raise OSError("helper pipe closed")
                view = view[written:]

        await asyncio.to_thread(write_all)
