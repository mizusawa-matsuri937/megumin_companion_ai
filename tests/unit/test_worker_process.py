"""Command and platform boundary tests for W12 process adapters."""

from __future__ import annotations

import ctypes
import os
import sys
from types import SimpleNamespace
from typing import Any

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


class _FakeWinCall:
    """ctypes-like callable used only for portable API-contract coverage."""

    def __init__(self, callback: Any) -> None:
        self._callback = callback
        self.argtypes: list[Any] = []
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        return self._callback(*args)


class _FakeKernel32:
    """In-memory kernel contract; never claimed as Job Object evidence."""

    def __init__(self) -> None:
        self._pipe_fds: set[int] = set()
        self._active_processes = 1
        self.closed_pseudo_handles: set[int] = set()
        self.CloseHandle = _FakeWinCall(self._close_handle)
        self.CreatePipe = _FakeWinCall(self._create_pipe)
        self.SetHandleInformation = _FakeWinCall(lambda *_args: 1)
        self.CreateJobObjectW = _FakeWinCall(lambda *_args: 10_001)
        self.SetInformationJobObject = _FakeWinCall(lambda *_args: 1)
        self.AssignProcessToJobObject = _FakeWinCall(lambda *_args: 1)
        self.InitializeProcThreadAttributeList = _FakeWinCall(self._initialize_attributes)
        self.UpdateProcThreadAttribute = _FakeWinCall(lambda *_args: 1)
        self.DeleteProcThreadAttributeList = _FakeWinCall(lambda *_args: None)
        self.CreateProcessW = _FakeWinCall(self._create_process)
        self.ResumeThread = _FakeWinCall(lambda *_args: 0)
        self.TerminateProcess = _FakeWinCall(self._terminate_process)
        self.WaitForSingleObject = _FakeWinCall(lambda *_args: 0)
        self.GetExitCodeProcess = _FakeWinCall(self._get_exit_code)
        self.TerminateJobObject = _FakeWinCall(self._terminate_process)
        self.QueryInformationJobObject = _FakeWinCall(self._query_job)

    @staticmethod
    def _value(handle: Any) -> int:
        value = handle.value if isinstance(handle, ctypes.c_void_p) else handle
        return 0 if value is None else int(value)

    def _create_pipe(self, read_pointer: Any, write_pointer: Any, *_args: Any) -> int:
        read_fd, write_fd = os.pipe()
        self._pipe_fds.update((read_fd, write_fd))
        read_pointer._obj.value = read_fd
        write_pointer._obj.value = write_fd
        return 1

    def _close_handle(self, handle: Any) -> int:
        value = self._value(handle)
        if value in self._pipe_fds:
            os.close(value)
            self._pipe_fds.remove(value)
        else:
            self.closed_pseudo_handles.add(value)
        return 1

    @staticmethod
    def _initialize_attributes(attribute_list: Any, *_args: Any) -> int:
        size_pointer = _args[-1]
        size_pointer._obj.value = 64
        return int(bool(attribute_list))

    @staticmethod
    def _create_process(*args: Any) -> int:
        process_info = args[-1]._obj
        process_info.hProcess = ctypes.c_void_p(10_002)
        process_info.hThread = ctypes.c_void_p(10_003)
        process_info.dwProcessId = 4242
        process_info.dwThreadId = 4243
        return 1

    def _terminate_process(self, *_args: Any) -> int:
        self._active_processes = 0
        return 1

    @staticmethod
    def _get_exit_code(_process: Any, code_pointer: Any) -> int:
        code_pointer._obj.value = 7
        return 1

    def _query_job(self, _job: Any, _kind: Any, accounting_pointer: Any, *_args: Any) -> int:
        accounting_pointer._obj.ActiveProcesses = self._active_processes
        return 1


def test_fake_kernel_contract_covers_portable_windows_handle_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise ctypes ownership on macOS without claiming native evidence."""

    async def scenario() -> None:
        kernel32 = _FakeKernel32()
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda handle, _flags: handle),
        )
        adapter = object.__new__(WindowsJobProcessAdapter)
        adapter._kernel32 = kernel32
        adapter._bind()

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

        process = await adapter.spawn((sys.executable, "-c", "pass"))
        assert process.pid == 4242 and process.create_no_window
        assert await process.read_stdout(1) == b""
        assert await process.read_stderr(1) == b""
        assert await process.wait() == 7
        assert await process.active_process_count() == 1
        await process.terminate_tree(23)
        assert await process.active_process_count() == 0
        await process.close()
        await process.close()
        assert await process.active_process_count() == 0
        await process.terminate_tree()
        with pytest.raises(ProcessAdapterError, match="worker_process_closed"):
            await process.write_stdin(b"synthetic")
        assert {10_001, 10_002, 10_003} <= kernel32.closed_pseudo_handles

    import asyncio

    asyncio.run(scenario())


def test_unsupported_adapter_discards_command_and_handle_inputs() -> None:
    async def scenario() -> None:
        adapter = UnsupportedProcessAdapter()
        with pytest.raises(ProcessAdapterError, match="platform_unsupported"):
            await adapter.spawn(("ignored",), inherited_handles=(1,))

    import asyncio

    asyncio.run(scenario())
