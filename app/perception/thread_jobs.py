"""Cancellation-safe ownership for privacy-sensitive thread jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
from typing import TypeVar

ResultT = TypeVar("ResultT")


def wipe_buffer(buffer: bytearray) -> None:
    """Best-effort zero and release of an owned mutable byte buffer."""

    buffer[:] = b"\x00" * len(buffer)
    buffer.clear()


async def await_owned_job(
    task: asyncio.Task[ResultT],
    *,
    discard: Callable[[ResultT], None],
) -> ResultT:
    """Await a thread job without allowing caller cancellation to orphan it.

    ``asyncio.to_thread`` cannot stop its worker when the awaiting task is
    cancelled.  Shield the worker, drain it before propagating cancellation,
    and destroy any result whose ownership can no longer be transferred.
    """

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        await _drain_task(task)
        if not task.cancelled():
            try:
                result = task.result()
            except BaseException:
                pass
            else:
                discard(result)
        raise cancellation


async def drain_owned_jobs(
    tasks: Collection[asyncio.Task[ResultT]],
    *,
    cleanup_events: Collection[asyncio.Event] = (),
) -> None:
    """Wait for workers and owner cleanup, even if the closer is cancelled."""

    if not tasks and not cleanup_events:
        return

    async def gather_jobs() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(event.wait() for event in cleanup_events))

    gather_task = asyncio.create_task(gather_jobs(), name="perception-thread-jobs-close")
    try:
        await asyncio.shield(gather_task)
    except asyncio.CancelledError as cancellation:
        await _drain_task(gather_task)
        raise cancellation


async def _drain_task(task: asyncio.Task[object]) -> None:
    """Drain one task while tolerating repeated cancellation requests."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
