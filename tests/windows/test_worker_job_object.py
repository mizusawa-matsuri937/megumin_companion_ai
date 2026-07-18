"""Native Windows evidence for W12 Job Object and CREATE_NO_WINDOW behavior."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app.workers.process import ManagedProcess, ProcessAdapterError, WindowsJobProcessAdapter
from app.workers.supervisor import SupervisorConfig, WorkerError, WorkerSupervisor

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires real Windows Job Objects")
_HELPER = Path(__file__).parents[1] / "helpers" / "w12_worker_helper.py"


def _process_alive(pid: int) -> bool:
    loader = getattr(ctypes, "WinDLL", None)
    assert callable(loader)
    kernel32 = loader("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x00100000, 0, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.WaitForSingleObject(handle, 0) == 258)
    finally:
        kernel32.CloseHandle(handle)


def _handle_count() -> int:
    loader = getattr(ctypes, "WinDLL", None)
    assert callable(loader)
    kernel32 = loader("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    count = ctypes.c_uint32()
    assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
    return int(count.value)


def _read_marker(marker: Path) -> dict[str, int]:
    payload: object = json.loads(marker.read_text(encoding="ascii"))
    if not isinstance(payload, dict):
        raise AssertionError("invalid marker")
    result: dict[str, int] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int):
            raise AssertionError("invalid marker")
        result[key] = value
    return result


async def _wait_marker(marker: Path) -> dict[str, int]:
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.01)
    return _read_marker(marker)


async def _wait_dead(pids: tuple[int, ...]) -> float:
    started = time.monotonic()
    async with asyncio.timeout(5):
        while any(_process_alive(pid) for pid in pids):
            await asyncio.sleep(0.01)
    return time.monotonic() - started


async def _wait_job_zero(process: ManagedProcess) -> None:
    async with asyncio.timeout(5):
        while await process.active_process_count() != 0:
            await asyncio.sleep(0.01)


def test_real_job_kills_worker_and_grandchild_and_returns_active_count_to_zero(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        marker = tmp_path / "tree.json"
        adapter = WindowsJobProcessAdapter()
        process = await adapter.spawn(
            (sys.executable, str(_HELPER), "--standalone-tree", str(marker))
        )
        identities = await _wait_marker(marker)
        active_count = await process.active_process_count()
        assert active_count is not None and active_count >= len(identities)
        assert all(_process_alive(pid) for pid in identities.values())
        started = time.monotonic()
        await process.terminate_tree(77)
        assert await process.wait() == 77
        kill_latency = time.monotonic() - started
        assert kill_latency < 2
        assert await process.active_process_count() == 0
        assert await _wait_dead(tuple(identities.values())) < 2
        await process.close()

    asyncio.run(scenario())


def test_real_supervisor_deadline_kills_tree_and_create_no_window_has_no_console(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        marker = tmp_path / "deadline-tree.json"
        supervisor = WorkerSupervisor(
            name="media-worker",
            role="media",
            command=(
                sys.executable,
                str(_HELPER),
                "--protocol-helper",
                "--marker",
                str(marker),
            ),
            adapter=WindowsJobProcessAdapter(),
            config=SupervisorConfig(
                handshake_timeout_seconds=2,
                heartbeat_timeout_seconds=1,
                soft_cancel_grace_seconds=0.1,
                terminate_wait_seconds=1,
                maximum_job_seconds=2,
                crash_budget=0,
                restart_backoff_initial_seconds=0.01,
                restart_backoff_max_seconds=0.01,
            ),
        )
        await supervisor.start()
        console = await supervisor.run_job(job_id="console", job_kind="console.check")
        assert console == {"console_window": 0}
        started = time.monotonic()
        with pytest.raises(WorkerError, match="worker_job_deadline"):
            await supervisor.run_job(
                job_id="tree",
                job_kind="tree.hang",
                hard_deadline_seconds=0.2,
            )
        identities = await _wait_marker(marker)
        assert time.monotonic() - started < 1.5
        assert await _wait_dead(tuple(identities.values())) < 1
        await supervisor.stop()

    asyncio.run(scenario())


def test_job_kill_on_close_survives_abrupt_parent_exit(tmp_path: Path) -> None:
    marker = tmp_path / "parent-crash-tree.json"
    probe = subprocess.Popen(
        [sys.executable, str(_HELPER), "--parent-crash-probe", str(marker)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert probe.wait(timeout=10) == 0
    identities = _read_marker(marker)
    assert asyncio.run(_wait_dead(tuple(identities.values()))) < 2


def test_only_explicit_additional_handle_is_inherited(tmp_path: Path) -> None:
    async def scenario() -> None:
        import msvcrt

        marker = tmp_path / "handle-read.json"
        read_fd, write_fd = os.pipe()
        get_osfhandle = getattr(msvcrt, "get_osfhandle", None)
        set_handle_inheritable = getattr(os, "set_handle_inheritable", None)
        assert callable(get_osfhandle) and callable(set_handle_inheritable)
        read_handle = get_osfhandle(read_fd)
        set_handle_inheritable(read_handle, True)
        process = await WindowsJobProcessAdapter().spawn(
            (
                sys.executable,
                str(_HELPER),
                "--inherited-handle",
                str(read_handle),
                "--handle-marker",
                str(marker),
            ),
            inherited_handles=(read_handle,),
        )
        os.close(read_fd)
        payload = b"synthetic-handle-payload"
        os.write(write_fd, payload)
        os.close(write_fd)
        assert await process.wait() == 0
        assert await _wait_marker(marker) == {"received_bytes": len(payload)}
        await _wait_job_zero(process)
        await process.close()
        with pytest.raises(ProcessAdapterError, match="process_closed"):
            await process.write_stdin(b"closed")

    asyncio.run(scenario())


def test_repeated_real_job_close_does_not_leak_parent_handles(tmp_path: Path) -> None:
    async def scenario() -> None:
        # Exclude one-time asyncio executor/thread handles from the Job leak metric.
        await asyncio.to_thread(lambda: None)
        baseline = _handle_count()
        for index in range(5):
            marker = tmp_path / f"handles-{index}.json"
            process = await WindowsJobProcessAdapter().spawn(
                (sys.executable, str(_HELPER), "--standalone-tree", str(marker))
            )
            await _wait_marker(marker)
            await process.terminate_tree()
            await process.wait()
            assert await process.active_process_count() == 0
            await process.close()
        await asyncio.sleep(0.1)
        assert _handle_count() <= baseline + 4

    asyncio.run(scenario())
