"""Command and platform boundary tests for W12 process adapters."""

from __future__ import annotations

import os
import sys

import pytest
from app.workers.process import (
    ProcessAdapterError,
    UnsupportedProcessAdapter,
    WindowsJobProcessAdapter,
    process_adapter_for_current_platform,
)


def test_current_platform_adapter_never_fakes_job_object_support() -> None:
    adapter = process_adapter_for_current_platform()
    assert adapter.supports_job_objects is (os.name == "nt")


def test_windows_adapter_rejects_command_injection_shapes_and_unapproved_handles() -> None:
    if os.name != "nt":
        with pytest.raises(ProcessAdapterError, match="platform_unsupported"):
            WindowsJobProcessAdapter()
        return

    async def scenario() -> None:
        adapter = WindowsJobProcessAdapter()
        for command in (
            (),
            ("relative-helper.exe",),
            (sys.executable, "bad\x00argument"),
            (sys.executable, *("x" for _ in range(128))),
            (sys.executable, "x" * 32768),
        ):
            with pytest.raises(ProcessAdapterError):
                await adapter.spawn(command)
        for handles in ((0,), (7, 7), tuple(range(1, 18))):
            with pytest.raises(ProcessAdapterError, match="inherited_handles_invalid"):
                await adapter.spawn((sys.executable, "-c", "pass"), inherited_handles=handles)

    import asyncio

    asyncio.run(scenario())


def test_unsupported_adapter_discards_command_and_handle_inputs() -> None:
    async def scenario() -> None:
        adapter = UnsupportedProcessAdapter()
        with pytest.raises(ProcessAdapterError, match="platform_unsupported"):
            await adapter.spawn(("ignored",), inherited_handles=(1,))

    import asyncio

    asyncio.run(scenario())
