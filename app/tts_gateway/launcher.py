"""User-started Windows launcher that owns the gateway process tree."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.workers.process import (
    ManagedProcess,
    ProcessAdapter,
    ProcessAdapterError,
    WindowsJobProcessAdapter,
)

_DRAIN_BYTES = 64 * 1024


async def run_launcher(
    manifest: Path,
    *,
    adapter: ProcessAdapter | None = None,
    python_executable: Path | None = None,
) -> int:
    """Run the gateway inside one unnamed kill-on-close Windows Job Object."""

    selected_adapter = adapter or WindowsJobProcessAdapter()
    if not selected_adapter.supports_job_objects:
        raise ProcessAdapterError("tts_gateway_job_object_required")
    executable = (python_executable or Path(sys.executable)).absolute()
    process = await selected_adapter.spawn(
        (
            str(executable),
            "-B",
            "-m",
            "app.tts_gateway.entrypoint",
            "--manifest",
            str(manifest.absolute()),
        )
    )
    stdout_drain = asyncio.create_task(
        _discard(process.read_stdout),
        name="tts-gateway-stdout-discard",
    )
    stderr_drain = asyncio.create_task(
        _discard(process.read_stderr),
        name="tts-gateway-stderr-discard",
    )
    try:
        return await process.wait()
    finally:
        await _finish_process(process, stdout_drain, stderr_drain)


async def _discard(reader: Callable[[int], Awaitable[bytes]]) -> None:
    try:
        while await reader(_DRAIN_BYTES):
            pass
    except (OSError, ProcessAdapterError):
        return


async def _finish_process(
    process: ManagedProcess,
    stdout_drain: asyncio.Task[None],
    stderr_drain: asyncio.Task[None],
) -> None:
    try:
        await process.terminate_tree()
    except ProcessAdapterError:
        pass
    finally:
        await process.close()
    for task in (stdout_drain, stderr_drain):
        if not task.done():
            task.cancel()
    await asyncio.gather(stdout_drain, stderr_drain, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        return asyncio.run(run_launcher(arguments.manifest))
    except KeyboardInterrupt:
        return 130
    except ProcessAdapterError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
