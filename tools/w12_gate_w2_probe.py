"""Emit path-free synthetic Gate W2 load and resource evidence.

This probe deliberately exercises only the already-merged W06 turn coordinator and
its bounded replay/idempotency behavior.  It does not connect any real provider,
audio, STT, VTS, or perception feature.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import CancellationToken, TurnService
from app.core.idempotency import InMemoryIdempotencyStore
from app.core.turns import SlowConsumerError
from app.schemas import PipelineEvent, TurnMetrics, TurnOutcome, TurnState, TurnStatus, UserMessage
from app.storage import SQLiteDatabase, SQLiteIdempotencyStore


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _rss_bytes() -> int:
    loader = getattr(ctypes, "WinDLL", None)
    if os.name == "nt" and callable(loader):
        kernel32 = loader("kernel32", use_last_error=True)
        psapi = loader("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.c_uint32,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise RuntimeError("rss_query_failed")
        return int(counters.WorkingSetSize)
    resource = importlib.import_module("resource")
    usage = resource.getrusage(resource.RUSAGE_SELF)
    scale = 1 if sys.platform == "darwin" else 1_024
    return int(usage.ru_maxrss) * scale


def _handle_count() -> int | None:
    loader = getattr(ctypes, "WinDLL", None)
    if os.name != "nt" or not callable(loader):
        return None
    kernel32 = loader("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int
    count = ctypes.c_uint32()
    if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
        raise RuntimeError("handle_count_failed")
    return int(count.value)


def _tree_bytes(root: Path) -> int:
    return sum(candidate.stat().st_size for candidate in root.rglob("*") if candidate.is_file())


def _logger() -> logging.Logger:
    logger = logging.getLogger("w12.gate_w2.synthetic")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def _message(index: int, *, session_id: str) -> UserMessage:
    return UserMessage(
        message_id=f"synthetic-message-{index}",
        session_id=session_id,
        text=f"synthetic input {index}",
        created_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
    )


class _MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class _CountingPipeline:
    def __init__(self) -> None:
        self.provider_calls = 0
        self.tts_calls = 0
        self.playback_calls = 0

    async def run(
        self,
        _message: UserMessage,
        _state: TurnState,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        self.provider_calls += 1
        token.raise_if_cancelled()
        await emit("assistant.delta", {"delta": "synthetic"})
        self.tts_calls += 1
        await emit("assistant.segment", {"text_length": 9})
        self.playback_calls += 1
        await emit("playback.started", {"index": 0})
        await emit("playback.finished", {"index": 0})
        return TurnOutcome(
            full_text="synthetic",
            segments=[],
            metrics=TurnMetrics(playback_count=1, segment_count=1),
        )

    async def close(self) -> None:
        return None


class _CountingObserver:
    def __init__(self) -> None:
        self.accepted = 0
        self.completed = 0

    async def on_user_accepted(self, _message: UserMessage, _state: TurnState) -> None:
        self.accepted += 1

    async def on_turn_completed(
        self,
        _message: UserMessage,
        _state: TurnState,
        _outcome: TurnOutcome,
    ) -> None:
        self.completed += 1


class _CountingSink:
    def __init__(self) -> None:
        self.action_calls = 0

    def publish(self, event: PipelineEvent) -> bool:
        if event.type == "assistant.segment":
            self.action_calls += 1
        return True

    async def close(self) -> None:
        return None


async def _turn_load(turn_count: int) -> dict[str, Any]:
    clock = _MutableClock()
    store = InMemoryIdempotencyStore(clock=clock)
    service = TurnService(_logger(), idempotency_store=store, clock=clock)
    rss_samples = [_rss_bytes()]
    handle_samples = [_handle_count()]
    thread_samples = [threading.active_count()]
    started = time.monotonic()
    for index in range(turn_count):
        state = await service.accept(_message(index, session_id="synthetic-10k"), client_id="gate")
        terminal = await service.cancel(
            client_id="gate", session_id="synthetic-10k", turn_id=state.turn_id
        )
        if terminal is None or terminal.status is not TurnStatus.cancelled:
            raise RuntimeError("turn_cancel_failed")
        if (index + 1) % 1_000 == 0:
            rss_samples.append(_rss_bytes())
            handle_samples.append(_handle_count())
            thread_samples.append(threading.active_count())

    snapshot = service.snapshot()
    retained = await store.count_records(client_id="gate", session_id="synthetic-10k")
    slow_service = TurnService(_logger(), subscriber_queue_limit=1)
    slow = await slow_service.subscribe("synthetic-slow", client_id="gate", last_seq=0)
    slow_state = await slow_service.accept(
        _message(1, session_id="synthetic-slow"), client_id="gate"
    )
    await slow_service.cancel(
        client_id="gate", session_id="synthetic-slow", turn_id=slow_state.turn_id
    )
    slow_disconnected = False
    try:
        await slow.get()
    except SlowConsumerError:
        slow_disconnected = True

    clock.now += timedelta(hours=24, microseconds=1)
    pruned = await service.accept(
        _message(turn_count + 1, session_id="synthetic-10k"), client_id="gate"
    )
    await service.cancel(client_id="gate", session_id="synthetic-10k", turn_id=pruned.turn_id)
    pruned_snapshot = service.snapshot()
    pruned_records = await store.count_records(client_id="gate", session_id="synthetic-10k")
    await asyncio.gather(service.shutdown(), slow_service.shutdown())
    rss_samples.append(_rss_bytes())
    handle_samples.append(_handle_count())
    thread_samples.append(threading.active_count())
    present_handles = [sample for sample in handle_samples if sample is not None]
    return {
        "turns": turn_count,
        "elapsed_ms": round((time.monotonic() - started) * 1_000, 3),
        "terminal_turns_bound": len(snapshot["turns"]),
        "replay_events_bound": snapshot["replay_event_count"],
        "idempotency_records_bound": retained,
        "post_ttl_terminal_turns": len(pruned_snapshot["turns"]),
        "post_ttl_idempotency_records": pruned_records,
        "slow_consumer_disconnected": slow_disconnected,
        "side_effect_calls": 0,
        "disk_bytes": 0,
        "rss_start_bytes": rss_samples[0],
        "rss_peak_bytes": max(rss_samples),
        "rss_end_bytes": rss_samples[-1],
        "handle_start": present_handles[0] if present_handles else None,
        "handle_peak": max(present_handles) if present_handles else None,
        "handle_end": present_handles[-1] if present_handles else None,
        "thread_peak": max(thread_samples),
        "thread_end": thread_samples[-1],
    }


async def _sqlite_claim_stress(iterations: int, concurrency: int) -> dict[str, Any]:
    failures: list[str] = []
    side_effect_totals: list[int] = []
    rss_samples = [_rss_bytes()]
    handle_samples = [_handle_count()]
    thread_samples = [threading.active_count()]
    maximum_database_bytes = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="w12-gate-synthetic-") as directory:
        root = Path(directory)
        for iteration in range(iterations):
            database = SQLiteDatabase(root / f"claim-{iteration}.sqlite3")
            database.initialize()
            pipelines = (_CountingPipeline(), _CountingPipeline())
            observers = (_CountingObserver(), _CountingObserver())
            sinks = (_CountingSink(), _CountingSink())
            services = tuple(
                TurnService(
                    _logger(),
                    pipelines[index],
                    observers=(observers[index],),
                    event_sinks=(sinks[index],),
                    idempotency_store=SQLiteIdempotencyStore(database),
                )
                for index in range(2)
            )
            try:
                message = _message(iteration, session_id=f"synthetic-sqlite-{iteration}")
                initial = await asyncio.gather(
                    *(
                        services[index % 2].accept(message, client_id="gate")
                        for index in range(concurrency)
                    )
                )
                await asyncio.gather(*(service.wait_idle() for service in services))
                terminal = await asyncio.gather(
                    *(
                        services[index % 2].accept(message, client_id="gate")
                        for index in range(concurrency)
                    )
                )
                turn_ids = {state.turn_id for state in (*initial, *terminal)}
                side_effects = [
                    sum(pipeline.provider_calls for pipeline in pipelines),
                    sum(pipeline.tts_calls for pipeline in pipelines),
                    sum(pipeline.playback_calls for pipeline in pipelines),
                    sum(sink.action_calls for sink in sinks),
                    sum(observer.accepted for observer in observers),
                    sum(observer.completed for observer in observers),
                ]
                side_effect_totals.extend(side_effects)
                if len(turn_ids) != 1 or any(
                    state.status is not TurnStatus.completed for state in terminal
                ):
                    failures.append("state_mismatch")
                if side_effects != [1, 1, 1, 1, 1, 1]:
                    failures.append("duplicate_side_effect")
            except (
                Exception
            ) as exc:  # Evidence must count every iteration, not stop at first failure.
                failures.append(type(exc).__name__)
            finally:
                await asyncio.gather(*(service.shutdown() for service in services))
            maximum_database_bytes = max(maximum_database_bytes, _tree_bytes(root))
            rss_samples.append(_rss_bytes())
            handle_samples.append(_handle_count())
            thread_samples.append(threading.active_count())
            for candidate in root.glob(f"claim-{iteration}.sqlite3*"):
                candidate.unlink()
        residual_before_context_cleanup = sum(1 for item in root.rglob("*") if item.is_file())
    present_handles = [sample for sample in handle_samples if sample is not None]
    return {
        "iterations": iterations,
        "concurrency_per_phase": concurrency,
        "accept_calls": iterations * concurrency * 2,
        "failure_count": len(failures),
        "failure_types": sorted(set(failures)),
        "side_effect_min": min(side_effect_totals) if side_effect_totals else None,
        "side_effect_max": max(side_effect_totals) if side_effect_totals else None,
        "maximum_database_bytes": maximum_database_bytes,
        "residual_files": residual_before_context_cleanup,
        "rss_start_bytes": rss_samples[0],
        "rss_peak_bytes": max(rss_samples),
        "rss_end_bytes": rss_samples[-1],
        "handle_start": present_handles[0] if present_handles else None,
        "handle_peak": max(present_handles) if present_handles else None,
        "handle_end": present_handles[-1] if present_handles else None,
        "thread_peak": max(thread_samples),
        "thread_end": thread_samples[-1],
        "elapsed_ms": round((time.monotonic() - started) * 1_000, 3),
    }


async def _run(turns: int, sqlite_iterations: int, sqlite_concurrency: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "input_class": "synthetic-only",
        "turn_load": await _turn_load(turns),
        "sqlite_claim_stress": await _sqlite_claim_stress(sqlite_iterations, sqlite_concurrency),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=10_000)
    parser.add_argument("--sqlite-iterations", type=int, default=100)
    parser.add_argument("--sqlite-concurrency", type=int, default=128)
    args = parser.parse_args()
    if args.turns < 10_000 or args.sqlite_iterations < 1 or args.sqlite_concurrency < 2:
        parser.error("probe limits are below the Gate W2 evidence floor")
    result = asyncio.run(_run(args.turns, args.sqlite_iterations, args.sqlite_concurrency))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return int(result["sqlite_claim_stress"]["failure_count"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
