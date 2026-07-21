"""Current-user, current-session single-instance activation for the desktop shell.

The secondary process deliberately has exactly one capability: signal that the
primary window should be shown.  It never opens a socket, accepts a command
payload, or forwards text from its command line.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Any, Protocol

from app.windows_security import (
    WindowsSecurityError,
    current_user_kernel_object_security_attributes,
    current_user_sid,
)

DEFAULT_INSTANCE_ID = "MeguminCompanion"
_SAFE_INSTANCE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0


class SingleInstanceError(RuntimeError):
    """A body-free failure to establish the desktop instance boundary."""


class InstanceRole(StrEnum):
    primary = "primary"
    secondary = "secondary"


class SingleInstance(Protocol):
    """Only expose the single safe cross-process operation: show the window."""

    def acquire(self) -> InstanceRole: ...

    def request_show(self) -> bool: ...

    def take_show_request(self) -> bool: ...

    def close(self) -> None: ...

    @property
    def recovery_marker_scope(self) -> str:
        """Opaque current-session identity for crash-recovery metadata."""


def windows_instance_name(*, instance_id: str, user_sid: str) -> str:
    """Return a Local namespace name without exposing the user SID itself.

    ``Local`` intentionally scopes the object to the current Windows session.
    A same-user RDP/fast-user-switch session therefore cannot activate a
    window in another session, while the DACL still prevents other users in
    this session from signaling the event.
    """

    _validate_instance_id(instance_id)
    if not user_sid:
        raise ValueError("user SID must not be empty")
    digest = hashlib.sha256(user_sid.encode("utf-8")).hexdigest()[:32]
    return f"Local\\{instance_id}.{digest}"


def recovery_marker_scope(*, user_sid: str, session_id: int) -> str:
    """Return an opaque marker scope for one user/session pair.

    ``Local`` kernel-object names are session-local, so two sessions of the
    same user may legitimately run separate desktop shells.  Their crash
    markers must therefore not overwrite or clear one another.  The on-disk
    filename uses this digest rather than a SID or session identifier.
    """

    if not user_sid:
        raise ValueError("user SID must not be empty")
    if session_id < 0:
        raise ValueError("session id must not be negative")
    material = f"{user_sid}\0{session_id}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


class WindowsCurrentUserInstance:  # pragma: no cover - exercised on Windows scenario gates
    """Named mutex plus auto-reset event protected by the current-user DACL."""

    def __init__(self, kernel32: Any, *, name: str, marker_scope: str) -> None:
        self._kernel32 = kernel32
        self._name = name
        self._recovery_marker_scope = marker_scope
        self._event_handle: int | None = None
        self._mutex_handle: int | None = None
        self._role: InstanceRole | None = None
        self._closed = False
        self._bind_kernel32()

    @classmethod
    def for_current_user(
        cls,
        *,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> WindowsCurrentUserInstance:
        if os.name != "nt":
            raise SingleInstanceError("single_instance_platform_unsupported")
        loader = getattr(ctypes, "WinDLL", None)
        if not callable(loader):
            raise SingleInstanceError("single_instance_platform_unsupported")
        try:
            sid = current_user_sid()
            kernel32 = loader("kernel32", use_last_error=True)
            session_id = _current_windows_session_id(kernel32)
        except (OSError, WindowsSecurityError) as exc:
            raise SingleInstanceError("single_instance_unavailable") from exc
        return cls(
            kernel32,
            name=windows_instance_name(instance_id=instance_id, user_sid=sid),
            marker_scope=recovery_marker_scope(user_sid=sid, session_id=session_id),
        )

    def acquire(self) -> InstanceRole:
        if self._closed:
            raise SingleInstanceError("single_instance_closed")
        if self._role is not None:
            return self._role
        try:
            # Create the event before publishing the mutex.  A secondary that
            # observes the mutex can therefore always signal the one allowed
            # activation event; it never needs a retrying IPC channel.
            with current_user_kernel_object_security_attributes() as attributes:
                event = self._kernel32.CreateEventW(
                    ctypes.byref(attributes),
                    0,
                    0,
                    self._event_name,
                )
                if not event:
                    raise self._native_error("single_instance_event_create_failed")
                _set_windows_last_error(0)
                mutex = self._kernel32.CreateMutexW(
                    ctypes.byref(attributes),
                    0,
                    self._name,
                )
                mutex_error = _windows_last_error()
                if not mutex:
                    self._close_handle(int(event))
                    raise self._native_error("single_instance_mutex_create_failed")
        except WindowsSecurityError as exc:
            raise SingleInstanceError("single_instance_unavailable") from exc

        if mutex_error == _ERROR_ALREADY_EXISTS:
            # A secondary retains only the event handle long enough to signal
            # it.  It releases the existing mutex handle immediately and has
            # no capability to send data or affect backend state.
            self._close_handle(int(mutex))
            self._event_handle = int(event)
            self._role = InstanceRole.secondary
            return self._role

        self._event_handle = int(event)
        self._mutex_handle = int(mutex)
        self._role = InstanceRole.primary
        return self._role

    def request_show(self) -> bool:
        if self._role is not InstanceRole.secondary or self._event_handle is None:
            return False
        return bool(self._kernel32.SetEvent(ctypes.c_void_p(self._event_handle)))

    def take_show_request(self) -> bool:
        if self._role is not InstanceRole.primary or self._event_handle is None:
            return False
        result = int(
            self._kernel32.WaitForSingleObject(
                ctypes.c_void_p(self._event_handle),
                0,
            )
        )
        if result == _WAIT_OBJECT_0:
            return True
        if result == 0x00000102:  # WAIT_TIMEOUT
            return False
        raise self._native_error("single_instance_event_wait_failed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        event_handle, mutex_handle = self._event_handle, self._mutex_handle
        self._event_handle = None
        self._mutex_handle = None
        if event_handle is not None:
            self._close_handle(event_handle)
        if mutex_handle is not None:
            self._close_handle(mutex_handle)

    @property
    def recovery_marker_scope(self) -> str:
        return self._recovery_marker_scope

    @property
    def _event_name(self) -> str:
        return self._name + ".show"

    def _bind_kernel32(self) -> None:
        self._kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        self._kernel32.CreateEventW.restype = ctypes.c_void_p
        self._kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        self._kernel32.SetEvent.restype = ctypes.c_int
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int

    def _close_handle(self, handle: int) -> None:
        with suppress(Exception):
            self._kernel32.CloseHandle(ctypes.c_void_p(handle))

    @staticmethod
    def _native_error(code: str) -> SingleInstanceError:
        # Do not surface raw OS detail in the UI.  The code is intentionally
        # stable and contains no object name, SID, path, or command content.
        return SingleInstanceError(code)


@dataclass(slots=True)
class _PortableSlot:
    show_requested: bool = False


_portable_lock = Lock()
_portable_slots: dict[str, _PortableSlot] = {}


class PortableCurrentSessionInstance:
    """Process-local fallback for non-Windows source-quality gates only.

    This is intentionally not offered as evidence for Windows DACL or session
    isolation.  It lets macOS/Linux CI exercise the same owner-graph decision
    without silently replacing the production Windows primitive.
    """

    def __init__(
        self,
        *,
        instance_id: str = DEFAULT_INSTANCE_ID,
        session_key: str = "process",
    ) -> None:
        _validate_instance_id(instance_id)
        if not session_key:
            raise ValueError("session key must not be empty")
        self._key = f"{instance_id}:{session_key}"
        self._recovery_marker_scope = hashlib.sha256(
            f"portable\0{session_key}".encode()
        ).hexdigest()[:32]
        self._slot: _PortableSlot | None = None
        self._role: InstanceRole | None = None
        self._closed = False

    def acquire(self) -> InstanceRole:
        if self._closed:
            raise SingleInstanceError("single_instance_closed")
        if self._role is not None:
            return self._role
        with _portable_lock:
            slot = _portable_slots.get(self._key)
            if slot is None:
                slot = _PortableSlot()
                _portable_slots[self._key] = slot
                self._role = InstanceRole.primary
            else:
                self._role = InstanceRole.secondary
            self._slot = slot
        return self._role

    def request_show(self) -> bool:
        if self._role is not InstanceRole.secondary or self._slot is None:
            return False
        with _portable_lock:
            if _portable_slots.get(self._key) is not self._slot:
                return False
            self._slot.show_requested = True
        return True

    def take_show_request(self) -> bool:
        if self._role is not InstanceRole.primary or self._slot is None:
            return False
        with _portable_lock:
            requested = self._slot.show_requested
            self._slot.show_requested = False
            return requested

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._role is InstanceRole.primary and self._slot is not None:
            with _portable_lock:
                if _portable_slots.get(self._key) is self._slot:
                    del _portable_slots[self._key]
        self._slot = None

    @property
    def recovery_marker_scope(self) -> str:
        return self._recovery_marker_scope


def single_instance_for_current_platform(
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
    portable_session_key: str = "process",
) -> SingleInstance:
    """Create the production primitive on Windows and a test-only fallback elsewhere."""

    if os.name == "nt":
        return WindowsCurrentUserInstance.for_current_user(instance_id=instance_id)
    return PortableCurrentSessionInstance(
        instance_id=instance_id,
        session_key=portable_session_key,
    )


def _validate_instance_id(value: str) -> None:
    if not _SAFE_INSTANCE_ID.fullmatch(value):
        raise ValueError("instance id must be a short safe identifier")


def _current_windows_session_id(kernel32: Any) -> int:
    """Return the current process session ID without using environment hints."""

    get_process_id = kernel32.GetCurrentProcessId
    get_process_id.argtypes = []
    get_process_id.restype = ctypes.c_uint32
    process_to_session = kernel32.ProcessIdToSessionId
    process_to_session.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    process_to_session.restype = ctypes.c_int
    session_id = ctypes.c_uint32()
    if not process_to_session(get_process_id(), ctypes.byref(session_id)):
        raise WindowsSecurityError("ProcessIdToSessionId failed")
    return int(session_id.value)


def _set_windows_last_error(value: int) -> None:
    setter = getattr(ctypes, "set_last_error", None)
    if not callable(setter):
        raise SingleInstanceError("single_instance_platform_unsupported")
    setter(value)


def _windows_last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    if not callable(getter):
        raise SingleInstanceError("single_instance_platform_unsupported")
    return int(getter())
