"""Per-user Windows startup registration for the desktop executable.

W15 intentionally uses only ``HKCU\\...\\Run``.  It never creates a scheduled
task, service, system-wide registry value, or elevated launcher.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "MeguminCompanion"


class StartupRegistrationError(RuntimeError):
    """Stable, content-free startup registration failure."""


class StartupState(StrEnum):
    unavailable = "unavailable"
    disabled = "disabled"
    enabled = "enabled"


@dataclass(frozen=True, slots=True)
class StartupReconcileResult:
    state: StartupState
    changed: bool


class RunValueStore(Protocol):
    """Minimal current-user Run value capability; no generic registry access."""

    def read(self, value_name: str) -> str | None: ...

    def write(self, value_name: str, value: str) -> None: ...

    def delete(self, value_name: str) -> bool: ...


class StartupRegistration:
    """Reconcile only this application's fixed HKCU value."""

    def __init__(self, store: RunValueStore) -> None:
        self._store = store

    def reconcile(self, *, enabled: bool, command: str) -> StartupReconcileResult:
        if not command.strip():
            raise ValueError("desktop startup command must not be empty")
        current = self._store.read(RUN_VALUE_NAME)
        if not enabled:
            return StartupReconcileResult(
                state=StartupState.disabled,
                changed=self._store.delete(RUN_VALUE_NAME) if current is not None else False,
            )
        if current == command:
            return StartupReconcileResult(state=StartupState.enabled, changed=False)
        self._store.write(RUN_VALUE_NAME, command)
        return StartupReconcileResult(state=StartupState.enabled, changed=True)

    def remove_for_uninstall(self) -> bool:
        """Delete only the fixed per-user value when W25 invokes uninstall cleanup."""

        return self._store.delete(RUN_VALUE_NAME)


class WindowsRunValueStore:  # pragma: no cover - exercised by Windows scenario gates
    """Narrow adapter around ``HKCU\\...\\Run``; never opens HKLM or Task Scheduler."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise StartupRegistrationError("startup_platform_unsupported")
        try:
            import winreg
        except ImportError as exc:
            raise StartupRegistrationError("startup_platform_unsupported") from exc
        # On macOS/Linux, typeshed intentionally exposes no Windows registry
        # members.  Runtime construction remains guarded by ``os.name``;
        # keeping the narrow adapter dynamic lets cross-platform strict mypy
        # check the source without pretending this capability is portable.
        self._winreg: Any = winreg

    def read(self, value_name: str) -> str | None:
        try:
            with self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                self._winreg.KEY_QUERY_VALUE,
            ) as key:
                value, value_type = self._winreg.QueryValueEx(key, value_name)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StartupRegistrationError("startup_registry_read_failed") from exc
        if value_type != self._winreg.REG_SZ or not isinstance(value, str):
            raise StartupRegistrationError("startup_registry_value_invalid")
        return value

    def write(self, value_name: str, value: str) -> None:
        try:
            with self._winreg.CreateKeyEx(
                self._winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                self._winreg.KEY_SET_VALUE,
            ) as key:
                self._winreg.SetValueEx(key, value_name, 0, self._winreg.REG_SZ, value)
        except OSError as exc:
            raise StartupRegistrationError("startup_registry_write_failed") from exc

    def delete(self, value_name: str) -> bool:
        try:
            with self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                self._winreg.KEY_SET_VALUE,
            ) as key:
                self._winreg.DeleteValue(key, value_name)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StartupRegistrationError("startup_registry_delete_failed") from exc
        return True


class UnsupportedStartupRegistration:
    """No-op non-Windows adapter; it is not evidence for Windows behavior."""

    def reconcile(self, *, enabled: bool, command: str) -> StartupReconcileResult:
        del enabled
        if not command.strip():
            raise ValueError("desktop startup command must not be empty")
        return StartupReconcileResult(state=StartupState.unavailable, changed=False)

    def remove_for_uninstall(self) -> bool:
        return False


def startup_registration_for_current_platform() -> (
    StartupRegistration | UnsupportedStartupRegistration
):
    if os.name == "nt":
        return StartupRegistration(WindowsRunValueStore())
    return UnsupportedStartupRegistration()


def desktop_startup_command(
    *,
    executable: Path | str | None = None,
    frozen: bool | None = None,
) -> str:
    """Build the exact current-user launch command without using the CWD.

    A frozen W24 onedir executable relaunches itself.  Source/wheel quality
    gates use the running interpreter plus ``-m desktop_client`` so a stale
    path can be detected and replaced during ``reconcile``.
    """

    program = str(Path(executable or sys.executable).resolve())
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    arguments = [program] if is_frozen else [program, "-m", "desktop_client"]
    return subprocess.list2cmdline(arguments)
