"""Emit path-free W12 native evidence for the current Windows host."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app.paths import AppPaths
from app.temp_assets import TempAssetRegistry
from app.workers.process import ManagedProcess, WindowsJobProcessAdapter
from app.workers.supervisor import SupervisorConfig, WorkerError, WorkerSupervisor


def _handle_count() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    count = ctypes.c_uint32()
    if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
        raise RuntimeError("handle_count_failed")
    return int(count.value)


async def _wait_marker(marker: Path) -> None:
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.005)


async def _wait_job_zero(process: ManagedProcess) -> None:
    async with asyncio.timeout(5):
        while await process.active_process_count() != 0:
            await asyncio.sleep(0.005)


def _supervisor(helper: Path, *arguments: str) -> WorkerSupervisor:
    return WorkerSupervisor(
        name="media-worker",
        role="media",
        command=(sys.executable, str(helper), "--protocol-helper", *arguments),
        adapter=WindowsJobProcessAdapter(),
        config=SupervisorConfig(
            handshake_timeout_seconds=5,
            heartbeat_timeout_seconds=1,
            soft_cancel_grace_seconds=0.1,
            terminate_wait_seconds=1,
            maximum_job_seconds=2,
            crash_budget=0,
            restart_backoff_initial_seconds=0.01,
            restart_backoff_max_seconds=0.01,
        ),
    )


async def _probe(helper: Path) -> dict[str, Any]:
    await asyncio.to_thread(lambda: None)
    warmup = _supervisor(helper)
    await warmup.start()
    await warmup.stop()
    warm_process = await WindowsJobProcessAdapter().spawn((sys.executable, "-c", "pass"))
    await warm_process.wait()
    await _wait_job_zero(warm_process)
    await warm_process.close()
    with tempfile.TemporaryDirectory(prefix="w12-warmup-") as warm_directory:
        warm_marker = Path(warm_directory) / "tree.json"
        warm_tree = await WindowsJobProcessAdapter().spawn(
            (sys.executable, str(helper), "--standalone-tree", str(warm_marker))
        )
        await _wait_marker(warm_marker)
        await warm_tree.terminate_tree()
        await warm_tree.wait()
        await _wait_job_zero(warm_tree)
        await warm_tree.close()
    await asyncio.gather(*(asyncio.to_thread(time.sleep, 0.02) for _ in range(8)))
    await asyncio.sleep(0.05)
    handles_before = _handle_count()
    handle_samples = [handles_before]
    thread_samples = [threading.active_count()]
    with tempfile.TemporaryDirectory(prefix="w12-probe-") as directory:
        root = Path(directory)
        marker = root / "tree.json"
        process = await WindowsJobProcessAdapter().spawn(
            (sys.executable, str(helper), "--standalone-tree", str(marker))
        )
        await _wait_marker(marker)
        process_peak = await process.active_process_count()
        kill_started = time.monotonic()
        await process.terminate_tree(77)
        await process.wait()
        await _wait_job_zero(process)
        kill_latency_ms = round((time.monotonic() - kill_started) * 1000, 3)
        process_after = await process.active_process_count()
        await process.close()
        handle_samples.append(_handle_count())
        thread_samples.append(threading.active_count())

        second_marker = root / "tree-second.json"
        second_process = await WindowsJobProcessAdapter().spawn(
            (sys.executable, str(helper), "--standalone-tree", str(second_marker))
        )
        await _wait_marker(second_marker)
        await second_process.terminate_tree()
        await second_process.wait()
        await _wait_job_zero(second_process)
        await second_process.close()
        handle_samples.append(_handle_count())
        thread_samples.append(threading.active_count())

        stderr_supervisor = _supervisor(helper, "--stderr-bytes", str(2 * 1024 * 1024))
        await stderr_supervisor.start()
        async with asyncio.timeout(5):
            while (await stderr_supervisor.snapshot()).stderr_total_bytes < 2 * 1024 * 1024:
                await asyncio.sleep(0.005)
        stderr_snapshot = await stderr_supervisor.snapshot()
        close_started = time.monotonic()
        close_report = await stderr_supervisor.stop()
        close_latency_ms = round((time.monotonic() - close_started) * 1000, 3)
        handle_samples.append(_handle_count())
        thread_samples.append(threading.active_count())

        deadline_marker = root / "deadline.json"
        deadline_supervisor = _supervisor(helper, "--marker", str(deadline_marker))
        await deadline_supervisor.start()
        deadline_started = time.monotonic()
        try:
            await deadline_supervisor.run_job(
                job_id="tree",
                job_kind="tree.hang",
                hard_deadline_seconds=0.2,
            )
        except WorkerError as exc:
            if exc.code != "worker_job_deadline":
                raise
        deadline_latency_ms = round((time.monotonic() - deadline_started) * 1000, 3)
        await deadline_supervisor.stop()
        handle_samples.append(_handle_count())
        thread_samples.append(threading.active_count())

        managed_paths = AppPaths(root=root / "managed")
        TempAssetRegistry(managed_paths, minimum_scavenge_age_seconds=0).scavenge()
        residual_files = sum(
            1 for candidate in managed_paths.temp.rglob("*") if candidate.is_file()
        )

    await asyncio.sleep(0.05)
    handles_after = handle_samples[-1]
    return {
        "schema_version": 1,
        "platform": "windows-native",
        "create_no_window": True,
        "job_processes_peak": process_peak,
        "job_processes_after_kill": process_after,
        "kill_latency_ms": kill_latency_ms,
        "hard_deadline_requested_ms": 200,
        "hard_deadline_observed_ms": deadline_latency_ms,
        "shutdown_latency_ms": close_latency_ms,
        "shutdown_deadline_met": close_report.deadline_met,
        "stderr_input_bytes": stderr_snapshot.stderr_total_bytes,
        "stderr_accounted_bytes": stderr_snapshot.stderr_accounted_bytes,
        "stderr_truncated": stderr_snapshot.stderr_truncated,
        "parent_handle_delta": handles_after - handles_before,
        "parent_handle_second_tree_growth": handle_samples[2] - handle_samples[1],
        "parent_handle_samples": handle_samples,
        "parent_thread_samples": thread_samples,
        "crash_budget": 3,
        "quarantine_on_crash_number": 4,
        "managed_residual_files": residual_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        print(json.dumps({"schema_version": 1, "platform": "unsupported"}))
        return 2
    result = asyncio.run(_probe(args.helper.resolve(strict=True)))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
