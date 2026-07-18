"""Synthetic subprocess used only by W12 native Windows scenario tests."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.workers.access import AuthorizedResource
from app.workers.helper import HelperRuntime
from app.workers.process import WindowsJobProcessAdapter


def _write_marker(path: Path, payload: dict[str, int]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="ascii")
    os.replace(temporary, path)


def _spawn_grandchild(marker: Path) -> subprocess.Popen[bytes]:
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--grandchild"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _write_marker(marker, {"worker_pid": os.getpid(), "grandchild_pid": child.pid})
    return child


class _SyntheticHandler:
    def __init__(self, marker: Path | None) -> None:
        self._marker = marker
        self._children: list[subprocess.Popen[bytes]] = []

    async def run_job(
        self,
        job_kind: str,
        resources: tuple[AuthorizedResource, ...],
        cancelled: asyncio.Event,
    ) -> dict[str, Any]:
        del resources
        if job_kind == "complete":
            return {"status": "ok"}
        if job_kind == "console.check":
            loader = getattr(ctypes, "WinDLL", None)
            if not callable(loader):
                raise RuntimeError("windows loader unavailable")
            kernel32 = loader("kernel32", use_last_error=True)
            kernel32.GetConsoleWindow.restype = ctypes.c_void_p
            return {"console_window": int(kernel32.GetConsoleWindow() or 0)}
        if job_kind == "tree.hang":
            if self._marker is None:
                raise RuntimeError("marker missing")
            self._children.append(_spawn_grandchild(self._marker))
            await asyncio.Event().wait()
        if job_kind == "cancel.wait":
            await cancelled.wait()
            return {}
        raise RuntimeError("unknown synthetic job")

    async def close(self) -> None:
        for child in self._children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=1)


async def _run_protocol_helper(marker: Path | None, stderr_bytes: int) -> int:
    if stderr_bytes:
        chunk = b"W12_SYNTHETIC_STDERR_SENTINEL" + b"X" * 8160
        remaining = stderr_bytes
        while remaining:
            selected = chunk[:remaining]
            os.write(sys.stderr.fileno(), selected)
            remaining -= len(selected)
    runtime = HelperRuntime(
        role="media",
        handler=_SyntheticHandler(marker),
        heartbeat_interval_seconds=0.02,
    )
    return await runtime.run()


def _standalone_tree(marker: Path) -> None:
    _spawn_grandchild(marker)
    while True:
        time.sleep(60)


def _read_inherited_handle(handle: int, marker: Path) -> None:
    import msvcrt

    open_osfhandle = getattr(msvcrt, "open_osfhandle", None)
    if not callable(open_osfhandle):
        raise RuntimeError("windows handle adapter unavailable")
    descriptor = open_osfhandle(handle, os.O_RDONLY)
    try:
        payload = os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
    _write_marker(marker, {"received_bytes": len(payload)})


async def _spawn_then_crash(marker: Path) -> None:
    adapter = WindowsJobProcessAdapter()
    await adapter.spawn(
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "--standalone-tree",
            str(marker),
        )
    )
    deadline = time.monotonic() + 5
    while not marker.exists():
        if time.monotonic() >= deadline:
            os._exit(91)
        await asyncio.sleep(0.01)
    os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol-helper", action="store_true")
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--stderr-bytes", type=int, default=0)
    parser.add_argument("--standalone-tree", type=Path)
    parser.add_argument("--parent-crash-probe", type=Path)
    parser.add_argument("--inherited-handle", type=int)
    parser.add_argument("--handle-marker", type=Path)
    parser.add_argument("--grandchild", action="store_true")
    args = parser.parse_args()
    if args.grandchild:
        while True:
            time.sleep(60)
    if args.standalone_tree is not None:
        _standalone_tree(args.standalone_tree)
    if args.parent_crash_probe is not None:
        asyncio.run(_spawn_then_crash(args.parent_crash_probe))
        return 92
    if args.inherited_handle is not None and args.handle_marker is not None:
        _read_inherited_handle(args.inherited_handle, args.handle_marker)
        return 0
    if args.protocol_helper:
        return asyncio.run(_run_protocol_helper(args.marker, args.stderr_bytes))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
