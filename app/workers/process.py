"""Platform process adapters for isolated helper workers.

The Windows implementation creates anonymous pipes, starts the helper
suspended with ``CREATE_NO_WINDOW``, assigns it to an unnamed Job Object, and
only then resumes its primary thread.  Closing the Job kills the full tree.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import stat
import subprocess
import threading
from collections.abc import Sequence
from contextlib import suppress
from ctypes import POINTER, Structure, byref, c_int, c_size_t, c_uint32, c_uint64, c_void_p
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.windows_security import ReparsePointError, assert_no_reparse_points

CREATE_NO_WINDOW = 0x08000000
_CREATE_SUSPENDED = 0x00000004
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE = 259
_MAX_COMMAND_ARGUMENTS = 128
_MAX_COMMAND_LINE_CHARS = 32767
_WINDOWS_KERNEL32: Any | None = None


class ProcessAdapterError(RuntimeError):
    """A stable platform-process error without command lines or paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@runtime_checkable
class ManagedProcess(Protocol):
    pid: int
    create_no_window: bool

    async def write_stdin(self, data: bytes) -> None: ...

    async def read_stdout(self, max_bytes: int) -> bytes: ...

    async def read_stderr(self, max_bytes: int) -> bytes: ...

    async def wait(self) -> int: ...

    async def terminate_tree(self, exit_code: int = 1) -> None: ...

    async def active_process_count(self) -> int | None: ...

    async def close(self) -> None: ...


@runtime_checkable
class ProcessAdapter(Protocol):
    @property
    def supports_job_objects(self) -> bool: ...

    async def spawn(
        self,
        command: Sequence[str],
        *,
        inherited_handles: Sequence[int] = (),
    ) -> ManagedProcess: ...


class UnsupportedProcessAdapter:
    """Portable fail-closed adapter; it never claims Windows Job capability."""

    @property
    def supports_job_objects(self) -> bool:
        return False

    async def spawn(
        self,
        command: Sequence[str],
        *,
        inherited_handles: Sequence[int] = (),
    ) -> ManagedProcess:
        del command, inherited_handles
        raise ProcessAdapterError("worker_platform_unsupported")


class _SecurityAttributes(Structure):
    _fields_ = [
        ("nLength", c_uint32),
        ("lpSecurityDescriptor", c_void_p),
        ("bInheritHandle", c_int),
    ]


class _StartupInfoW(Structure):
    _fields_ = [
        ("cb", c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", c_uint32),
        ("dwY", c_uint32),
        ("dwXSize", c_uint32),
        ("dwYSize", c_uint32),
        ("dwXCountChars", c_uint32),
        ("dwYCountChars", c_uint32),
        ("dwFillAttribute", c_uint32),
        ("dwFlags", c_uint32),
        ("wShowWindow", ctypes.c_ushort),
        ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", POINTER(ctypes.c_ubyte)),
        ("hStdInput", c_void_p),
        ("hStdOutput", c_void_p),
        ("hStdError", c_void_p),
    ]


class _StartupInfoExW(Structure):
    _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", c_void_p)]


class _ProcessInformation(Structure):
    _fields_ = [
        ("hProcess", c_void_p),
        ("hThread", c_void_p),
        ("dwProcessId", c_uint32),
        ("dwThreadId", c_uint32),
    ]


class _IoCounters(Structure):
    _fields_ = [
        ("ReadOperationCount", c_uint64),
        ("WriteOperationCount", c_uint64),
        ("OtherOperationCount", c_uint64),
        ("ReadTransferCount", c_uint64),
        ("WriteTransferCount", c_uint64),
        ("OtherTransferCount", c_uint64),
    ]


class _BasicLimitInformation(Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", c_uint32),
        ("MinimumWorkingSetSize", c_size_t),
        ("MaximumWorkingSetSize", c_size_t),
        ("ActiveProcessLimit", c_uint32),
        ("Affinity", c_size_t),
        ("PriorityClass", c_uint32),
        ("SchedulingClass", c_uint32),
    ]


class _ExtendedLimitInformation(Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", c_size_t),
        ("JobMemoryLimit", c_size_t),
        ("PeakProcessMemoryUsed", c_size_t),
        ("PeakJobMemoryUsed", c_size_t),
    ]


class _BasicAccountingInformation(Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", c_uint32),
        ("TotalProcesses", c_uint32),
        ("ActiveProcesses", c_uint32),
        ("TotalTerminatedProcesses", c_uint32),
    ]


def _last_error_code() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if callable(getter) else 0


def _load_kernel32() -> Any:
    global _WINDOWS_KERNEL32  # noqa: PLW0603 - one process-owned DLL binding
    if _WINDOWS_KERNEL32 is None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise ProcessAdapterError("worker_platform_unsupported")
        _WINDOWS_KERNEL32 = loader("kernel32", use_last_error=True)
    return _WINDOWS_KERNEL32


def _win_error(operation: str) -> ProcessAdapterError:
    return ProcessAdapterError(f"{operation}_failed_{_last_error_code()}")


def _close_handle(kernel32: Any, handle: int | c_void_p | None) -> bool:
    value = handle.value if isinstance(handle, c_void_p) else handle
    if value:
        return bool(kernel32.CloseHandle(c_void_p(value)))
    return True


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(command)
    if (
        not selected
        or len(selected) > _MAX_COMMAND_ARGUMENTS
        or any(not isinstance(item, str) or not item or "\x00" in item for item in selected)
    ):
        raise ProcessAdapterError("worker_command_invalid")
    command_line = subprocess.list2cmdline(selected)
    if len(command_line) > _MAX_COMMAND_LINE_CHARS:
        raise ProcessAdapterError("worker_command_too_long")
    executable = Path(selected[0])
    if not executable.is_absolute():
        raise ProcessAdapterError("worker_executable_not_absolute")
    try:
        canonical = executable.resolve(strict=True)
        if not stat.S_ISREG(canonical.stat().st_mode):
            raise ProcessAdapterError("worker_executable_invalid")
        assert_no_reparse_points(canonical.parent, canonical)
    except (OSError, ReparsePointError) as exc:
        raise ProcessAdapterError("worker_executable_invalid") from exc
    return (str(canonical), *selected[1:])


@dataclass(slots=True)
class _WindowsPipeSet:
    stdin_read: int = 0
    stdin_write: int = 0
    stdout_read: int = 0
    stdout_write: int = 0
    stderr_read: int = 0
    stderr_write: int = 0

    def all_handles(self) -> tuple[int, ...]:
        return (
            self.stdin_read,
            self.stdin_write,
            self.stdout_read,
            self.stdout_write,
            self.stderr_read,
            self.stderr_write,
        )


class WindowsJobProcessAdapter:
    """Create a helper atomically inside an unnamed kill-on-close Job Object."""

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise ProcessAdapterError("worker_platform_unsupported")
        self._kernel32 = _load_kernel32()
        self._bind()

    @property
    def supports_job_objects(self) -> bool:
        return True

    def _bind(self) -> None:
        k32 = self._kernel32
        k32.CloseHandle.argtypes = [c_void_p]
        k32.CloseHandle.restype = c_int
        k32.CreatePipe.argtypes = [
            POINTER(c_void_p),
            POINTER(c_void_p),
            POINTER(_SecurityAttributes),
            c_uint32,
        ]
        k32.CreatePipe.restype = c_int
        k32.SetHandleInformation.argtypes = [c_void_p, c_uint32, c_uint32]
        k32.SetHandleInformation.restype = c_int
        k32.CreateJobObjectW.argtypes = [c_void_p, ctypes.c_wchar_p]
        k32.CreateJobObjectW.restype = c_void_p
        k32.SetInformationJobObject.argtypes = [c_void_p, c_int, c_void_p, c_uint32]
        k32.SetInformationJobObject.restype = c_int
        k32.AssignProcessToJobObject.argtypes = [c_void_p, c_void_p]
        k32.AssignProcessToJobObject.restype = c_int
        k32.InitializeProcThreadAttributeList.argtypes = [
            c_void_p,
            c_uint32,
            c_uint32,
            POINTER(c_size_t),
        ]
        k32.InitializeProcThreadAttributeList.restype = c_int
        k32.UpdateProcThreadAttribute.argtypes = [
            c_void_p,
            c_uint32,
            c_size_t,
            c_void_p,
            c_size_t,
            c_void_p,
            c_void_p,
        ]
        k32.UpdateProcThreadAttribute.restype = c_int
        k32.DeleteProcThreadAttributeList.argtypes = [c_void_p]
        k32.CreateProcessW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            c_void_p,
            c_void_p,
            c_int,
            c_uint32,
            c_void_p,
            ctypes.c_wchar_p,
            POINTER(_StartupInfoExW),
            POINTER(_ProcessInformation),
        ]
        k32.CreateProcessW.restype = c_int
        k32.ResumeThread.argtypes = [c_void_p]
        k32.ResumeThread.restype = c_uint32
        k32.TerminateProcess.argtypes = [c_void_p, c_uint32]
        k32.TerminateProcess.restype = c_int

    async def spawn(
        self,
        command: Sequence[str],
        *,
        inherited_handles: Sequence[int] = (),
    ) -> ManagedProcess:
        selected = _validate_command(command)
        selected_handles = tuple(inherited_handles)
        if (
            len(selected_handles) > 16
            or len(set(selected_handles)) != len(selected_handles)
            or any(isinstance(handle, bool) or handle <= 0 for handle in selected_handles)
        ):
            raise ProcessAdapterError("worker_inherited_handles_invalid")
        return await asyncio.to_thread(self._spawn_sync, selected, selected_handles)

    def _pipe(self, pipes: _WindowsPipeSet, read_name: str, write_name: str) -> None:
        read_handle = c_void_p()
        write_handle = c_void_p()
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, 1)
        if not self._kernel32.CreatePipe(
            byref(read_handle), byref(write_handle), byref(attributes), 0
        ):
            raise _win_error("worker_pipe_create")
        setattr(pipes, read_name, int(read_handle.value or 0))
        setattr(pipes, write_name, int(write_handle.value or 0))

    def _spawn_sync(
        self,
        command: tuple[str, ...],
        inherited_handles: tuple[int, ...],
    ) -> ManagedProcess:
        import msvcrt

        k32 = self._kernel32
        pipes = _WindowsPipeSet()
        job = 0
        info = _ProcessInformation()
        attribute_buffer: Any | None = None
        attribute_list = c_void_p()
        converted_fds: list[int] = []
        try:
            self._pipe(pipes, "stdin_read", "stdin_write")
            self._pipe(pipes, "stdout_read", "stdout_write")
            self._pipe(pipes, "stderr_read", "stderr_write")
            for parent_handle in (pipes.stdin_write, pipes.stdout_read, pipes.stderr_read):
                if not k32.SetHandleInformation(c_void_p(parent_handle), _HANDLE_FLAG_INHERIT, 0):
                    raise _win_error("worker_pipe_inheritance")

            job_value = k32.CreateJobObjectW(None, None)
            if not job_value:
                raise _win_error("worker_job_create")
            job = int(job_value)
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                c_void_p(job),
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                byref(limits),
                ctypes.sizeof(limits),
            ):
                raise _win_error("worker_job_limit")

            size = c_size_t()
            k32.InitializeProcThreadAttributeList(None, 1, 0, byref(size))
            attribute_buffer = ctypes.create_string_buffer(size.value)
            attribute_list = ctypes.cast(attribute_buffer, c_void_p)
            if not k32.InitializeProcThreadAttributeList(attribute_list, 1, 0, byref(size)):
                raise _win_error("worker_attribute_list")
            inherited_values = (
                pipes.stdin_read,
                pipes.stdout_write,
                pipes.stderr_write,
                *inherited_handles,
            )
            inherited = (c_void_p * len(inherited_values))(*inherited_values)
            if not k32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(inherited, c_void_p),
                ctypes.sizeof(inherited),
                None,
                None,
            ):
                raise _win_error("worker_handle_list")

            startup = _StartupInfoExW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = c_void_p(pipes.stdin_read)
            startup.StartupInfo.hStdOutput = c_void_p(pipes.stdout_write)
            startup.StartupInfo.hStdError = c_void_p(pipes.stderr_write)
            startup.lpAttributeList = attribute_list
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            flags = CREATE_NO_WINDOW | _CREATE_SUSPENDED | _EXTENDED_STARTUPINFO_PRESENT
            if not k32.CreateProcessW(
                command[0],
                command_line,
                None,
                None,
                1,
                flags,
                None,
                None,
                byref(startup),
                byref(info),
            ):
                raise _win_error("worker_process_create")
            if not k32.AssignProcessToJobObject(c_void_p(job), info.hProcess):
                raise _win_error("worker_job_assign")
            if k32.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise _win_error("worker_thread_resume")
            if not _close_handle(k32, info.hThread):
                raise _win_error("worker_handle_close")
            info.hThread = None
            for child_handle_name in ("stdin_read", "stdout_write", "stderr_write"):
                if not _close_handle(k32, getattr(pipes, child_handle_name)):
                    raise _win_error("worker_handle_close")
                setattr(pipes, child_handle_name, 0)
            stdin_fd = msvcrt.open_osfhandle(pipes.stdin_write, os.O_BINARY)
            converted_fds.append(stdin_fd)
            pipes.stdin_write = 0
            stdout_fd = msvcrt.open_osfhandle(pipes.stdout_read, os.O_BINARY)
            converted_fds.append(stdout_fd)
            pipes.stdout_read = 0
            stderr_fd = msvcrt.open_osfhandle(pipes.stderr_read, os.O_BINARY)
            converted_fds.append(stderr_fd)
            pipes.stderr_read = 0
            process = _WindowsManagedProcess(
                k32, int(info.hProcess), job, int(info.dwProcessId), stdin_fd, stdout_fd, stderr_fd
            )
            converted_fds.clear()
            return process
        except Exception:
            if info.hProcess:
                with suppress(Exception):
                    k32.TerminateProcess(info.hProcess, 1)
            for fd in converted_fds:
                with suppress(OSError):
                    os.close(fd)
            _close_handle(k32, info.hThread)
            _close_handle(k32, info.hProcess)
            _close_handle(k32, job)
            for handle in pipes.all_handles():
                _close_handle(k32, handle)
            raise
        finally:
            if attribute_list.value:
                k32.DeleteProcThreadAttributeList(attribute_list)


class _WindowsManagedProcess:
    create_no_window = True

    def __init__(
        self,
        kernel32: Any,
        process: int,
        job: int,
        pid: int,
        stdin_fd: int,
        stdout_fd: int,
        stderr_fd: int,
    ) -> None:
        self._kernel32 = kernel32
        self._process = process
        self._job = job
        self.pid = pid
        self._stdin_fd = stdin_fd
        self._stdout_fd = stdout_fd
        self._stderr_fd = stderr_fd
        self._write_lock: threading.Lock | None = threading.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._bind()

    def _bind(self) -> None:
        k32 = self._kernel32
        k32.WaitForSingleObject.argtypes = [c_void_p, c_uint32]
        k32.WaitForSingleObject.restype = c_uint32
        k32.GetExitCodeProcess.argtypes = [c_void_p, POINTER(c_uint32)]
        k32.GetExitCodeProcess.restype = c_int
        k32.TerminateJobObject.argtypes = [c_void_p, c_uint32]
        k32.TerminateJobObject.restype = c_int
        k32.QueryInformationJobObject.argtypes = [
            c_void_p,
            c_int,
            c_void_p,
            c_uint32,
            POINTER(c_uint32),
        ]
        k32.QueryInformationJobObject.restype = c_int

    async def write_stdin(self, data: bytes) -> None:
        await asyncio.to_thread(self._write_all, data)

    def _write_all(self, data: bytes) -> None:
        lock = self._write_lock
        if lock is None:
            raise ProcessAdapterError("worker_process_closed")
        with lock:
            view = memoryview(data)
            while view:
                written = os.write(self._stdin_fd, view)
                if written <= 0:
                    raise ProcessAdapterError("worker_pipe_write_failed")
                view = view[written:]

    async def read_stdout(self, max_bytes: int) -> bytes:
        return await asyncio.to_thread(os.read, self._stdout_fd, max_bytes)

    async def read_stderr(self, max_bytes: int) -> bytes:
        return await asyncio.to_thread(os.read, self._stderr_fd, max_bytes)

    async def wait(self) -> int:
        return await asyncio.to_thread(self._wait_sync)

    def _wait_sync(self) -> int:
        result = self._kernel32.WaitForSingleObject(c_void_p(self._process), _INFINITE)
        if result != _WAIT_OBJECT_0:
            raise _win_error("worker_process_wait")
        code = c_uint32()
        if not self._kernel32.GetExitCodeProcess(c_void_p(self._process), byref(code)):
            raise _win_error("worker_exit_code")
        return int(code.value)

    async def terminate_tree(self, exit_code: int = 1) -> None:
        if self._job and not self._kernel32.TerminateJobObject(c_void_p(self._job), exit_code):
            error = _last_error_code()
            if error not in {5, 6}:
                raise ProcessAdapterError(f"worker_job_terminate_failed_{error}")

    async def active_process_count(self) -> int | None:
        if not self._job:
            return 0
        accounting = _BasicAccountingInformation()
        returned = c_uint32()
        if not self._kernel32.QueryInformationJobObject(
            c_void_p(self._job),
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            byref(accounting),
            ctypes.sizeof(accounting),
            byref(returned),
        ):
            raise _win_error("worker_job_query")
        return int(accounting.ActiveProcesses)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            close_failed = False
            for fd_name in ("_stdin_fd", "_stdout_fd", "_stderr_fd"):
                fd = getattr(self, fd_name)
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        close_failed = True
                    setattr(self, fd_name, -1)
            if self._job:
                close_failed = not _close_handle(self._kernel32, self._job) or close_failed
                self._job = 0
            if self._process:
                close_failed = not _close_handle(self._kernel32, self._process) or close_failed
                self._process = 0
            self._write_lock = None
            if close_failed:
                raise ProcessAdapterError("worker_handle_close_failed")


def process_adapter_for_current_platform() -> ProcessAdapter:
    if os.name == "nt":
        return WindowsJobProcessAdapter()
    return UnsupportedProcessAdapter()
