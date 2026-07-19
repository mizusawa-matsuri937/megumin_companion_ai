"""Command and platform boundary tests for W12 process adapters."""

from __future__ import annotations

import asyncio
import ctypes
import os
import queue
import sys
import threading
from types import SimpleNamespace
from typing import Any, cast

import app.workers.process as worker_process
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
        for handles in ((0,), (7, 7), tuple(range(1, 18)), (1.5,)):
            with pytest.raises(ProcessAdapterError, match="inherited_handles_invalid"):
                await adapter.spawn(
                    (sys.executable, "-c", "pass"),
                    inherited_handles=handles,  # type: ignore[arg-type]
                )

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
        monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
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

        for function_name, failure_value, error_code in (
            ("CreatePipe", 0, "worker_pipe_create_failed_0"),
            ("SetHandleInformation", 0, "worker_pipe_inheritance_failed_0"),
            ("CreateJobObjectW", 0, "worker_job_create_failed_0"),
            ("SetInformationJobObject", 0, "worker_job_limit_failed_0"),
            ("InitializeProcThreadAttributeList", 0, "worker_attribute_list_failed_0"),
            ("UpdateProcThreadAttribute", 0, "worker_handle_list_failed_0"),
            ("CreateProcessW", 0, "worker_process_create_failed_0"),
            ("AssignProcessToJobObject", 0, "worker_job_assign_failed_0"),
            ("ResumeThread", 0xFFFFFFFF, "worker_thread_resume_failed_0"),
        ):
            failure_kernel = _FakeKernel32()
            setattr(
                failure_kernel,
                function_name,
                _FakeWinCall(lambda *_args, value=failure_value: value),
            )
            failure_adapter = object.__new__(WindowsJobProcessAdapter)
            failure_adapter._kernel32 = failure_kernel
            failure_adapter._bind()
            with pytest.raises(ProcessAdapterError, match=error_code):
                await failure_adapter.spawn((sys.executable, "-c", "pass"))
            assert not failure_kernel._pipe_fds

    asyncio.run(scenario())


def test_fake_kernel_managed_process_errors_remain_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise native error mapping portably without claiming Job evidence."""

    async def scenario() -> None:
        kernel32 = _FakeKernel32()
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda handle, _flags: handle),
        )
        monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
        adapter = object.__new__(WindowsJobProcessAdapter)
        adapter._kernel32 = kernel32
        adapter._bind()
        process = await adapter.spawn((sys.executable, "-c", "pass"))

        waits = iter((258, 0))
        kernel32.WaitForSingleObject = _FakeWinCall(lambda *_args: next(waits))
        assert await process.wait() == 7

        kernel32.WaitForSingleObject = _FakeWinCall(lambda *_args: 0)
        kernel32.GetExitCodeProcess = _FakeWinCall(lambda *_args: 0)
        monkeypatch.setattr(worker_process, "_last_error_code", lambda: 0)
        with pytest.raises(ProcessAdapterError, match="worker_exit_code_failed_0"):
            await process.wait()

        kernel32.TerminateJobObject = _FakeWinCall(lambda *_args: 0)
        monkeypatch.setattr(worker_process, "_last_error_code", lambda: 5)
        await process.terminate_tree()
        monkeypatch.setattr(worker_process, "_last_error_code", lambda: 123)
        with pytest.raises(ProcessAdapterError, match="worker_job_terminate_failed_123"):
            await process.terminate_tree()

        kernel32.QueryInformationJobObject = _FakeWinCall(lambda *_args: 0)
        with pytest.raises(ProcessAdapterError, match="worker_job_query_failed_123"):
            await process.active_process_count()

        kernel32.CloseHandle = _FakeWinCall(lambda *_args: 0)
        with pytest.raises(ProcessAdapterError, match="worker_handle_close_failed"):
            await process.close()
        await process.close()

    asyncio.run(scenario())


def test_fake_kernel_managed_process_edge_ownership_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover bounded close/report edges portably, not as native evidence."""

    class DeadWriter:
        def join(self, *, timeout: float) -> None:
            assert timeout == 0.05

        def is_alive(self) -> bool:
            return False

    async def scenario() -> None:
        kernel32 = _FakeKernel32()
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda handle, _flags: handle),
        )
        monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
        adapter = object.__new__(WindowsJobProcessAdapter)
        adapter._kernel32 = kernel32
        adapter._bind()
        process = await adapter.spawn((sys.executable, "-c", "pass"))
        managed = cast(Any, process)

        kernel32.WaitForSingleObject = _FakeWinCall(lambda *_args: 999)
        monkeypatch.setattr(worker_process, "_last_error_code", lambda: 44)
        with pytest.raises(ProcessAdapterError, match="worker_process_wait_failed_44"):
            await process.wait()

        monkeypatch.setattr(os, "write", lambda *_args: 0)
        with pytest.raises(ProcessAdapterError, match="worker_pipe_write_failed"):
            managed._write_all(b"bounded")

        stopping = threading.Event()
        stopping.set()
        managed._writer_thread = DeadWriter()
        managed._writer_stopping = stopping
        managed._write_queue = queue.Queue(maxsize=1)
        with pytest.raises(ProcessAdapterError, match="worker_process_closed"):
            await process.write_stdin(b"rejected")

        stopping.clear()
        pending: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        managed._write_queue.put_nowait(
            SimpleNamespace(loop=asyncio.get_running_loop(), completed=pending)
        )
        await process.close()
        with pytest.raises(ProcessAdapterError, match="worker_process_closed"):
            await pending
        managed._write_loop()
        assert managed._write_queue is None
        assert managed._writer_stopping is None

    asyncio.run(scenario())


def test_fake_kernel_late_spawn_failures_release_partial_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover late handle/fd cleanup portably, not as native evidence."""

    async def spawn_with(kernel32: _FakeKernel32, open_osfhandle: Any) -> None:
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=open_osfhandle),
        )
        monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
        adapter = object.__new__(WindowsJobProcessAdapter)
        adapter._kernel32 = kernel32
        adapter._bind()
        await adapter.spawn((sys.executable, "-c", "pass"))

    async def scenario() -> None:
        thread_kernel = _FakeKernel32()
        real_thread_close = thread_kernel._close_handle

        def fail_thread_close(handle: Any) -> int:
            if thread_kernel._value(handle) == 10_003:
                return 0
            return real_thread_close(handle)

        thread_kernel.CloseHandle = _FakeWinCall(fail_thread_close)
        with pytest.raises(ProcessAdapterError, match="worker_handle_close_failed_0"):
            await spawn_with(thread_kernel, lambda handle, _flags: handle)
        assert not thread_kernel._pipe_fds

        child_kernel = _FakeKernel32()
        real_child_close = child_kernel._close_handle
        failed_once = False

        def fail_one_child_close(handle: Any) -> int:
            nonlocal failed_once
            if child_kernel._value(handle) in child_kernel._pipe_fds and not failed_once:
                failed_once = True
                return 0
            return real_child_close(handle)

        child_kernel.CloseHandle = _FakeWinCall(fail_one_child_close)
        with pytest.raises(ProcessAdapterError, match="worker_handle_close_failed_0"):
            await spawn_with(child_kernel, lambda handle, _flags: handle)
        assert not child_kernel._pipe_fds

        conversion_kernel = _FakeKernel32()
        converted = 0

        def fail_second_conversion(handle: int, _flags: int) -> int:
            nonlocal converted
            converted += 1
            conversion_kernel._pipe_fds.remove(handle)
            if converted == 2:
                raise OSError("synthetic fd conversion failure")
            return handle

        with pytest.raises(OSError, match="synthetic fd conversion failure"):
            await spawn_with(conversion_kernel, fail_second_conversion)
        assert not conversion_kernel._pipe_fds

    asyncio.run(scenario())


def test_fake_kernel_writer_backpressure_and_cleanup_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover writer ownership portably without claiming native Job evidence."""

    async def scenario() -> None:
        kernel32 = _FakeKernel32()
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda handle, _flags: handle),
        )
        monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
        adapter = object.__new__(WindowsJobProcessAdapter)
        adapter._kernel32 = kernel32
        adapter._bind()
        process = await adapter.spawn((sys.executable, "-c", "pass"))

        entered = threading.Event()
        release = threading.Event()

        def blocking_write(_fd: int, data: bytes | memoryview) -> int:
            entered.set()
            assert release.wait(timeout=1)
            return len(data)

        monkeypatch.setattr(os, "write", blocking_write)
        first = asyncio.create_task(process.write_stdin(b"first"))
        assert await asyncio.to_thread(entered.wait, 0.2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(process.write_stdin(b"second"))
        await asyncio.sleep(0)
        with pytest.raises(ProcessAdapterError, match="worker_pipe_write_busy"):
            await process.write_stdin(b"third")
        release.set()
        await asyncio.wait_for(second, timeout=0.2)

        def fail_write(_fd: int, _data: bytes | memoryview) -> int:
            raise OSError("synthetic broken pipe")

        monkeypatch.setattr(os, "write", fail_write)
        with pytest.raises(ProcessAdapterError, match="worker_pipe_write_failed"):
            await process.write_stdin(b"failure")

        await process.terminate_tree()
        await process.close()
        managed = cast(Any, process)
        assert managed._writer_thread is None
        assert managed._write_queue is None
        assert managed._writer_stopping is None

    asyncio.run(scenario())


def test_unsupported_adapter_discards_command_and_handle_inputs() -> None:
    async def scenario() -> None:
        adapter = UnsupportedProcessAdapter()
        with pytest.raises(ProcessAdapterError, match="platform_unsupported"):
            await adapter.spawn(("ignored",), inherited_handles=(1,))

    asyncio.run(scenario())
