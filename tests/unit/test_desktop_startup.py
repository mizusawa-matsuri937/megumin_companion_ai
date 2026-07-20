from __future__ import annotations

from pathlib import Path

from desktop_client.startup import (
    RUN_VALUE_NAME,
    StartupRegistration,
    StartupState,
    UnsupportedStartupRegistration,
    desktop_startup_command,
)


class _MemoryRunStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.writes: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    def read(self, value_name: str) -> str | None:
        return self.values.get(value_name)

    def write(self, value_name: str, value: str) -> None:
        self.values[value_name] = value
        self.writes.append((value_name, value))

    def delete(self, value_name: str) -> bool:
        self.deletes.append(value_name)
        return self.values.pop(value_name, None) is not None


def test_startup_is_disabled_by_default_and_removes_only_its_stale_run_value() -> None:
    store = _MemoryRunStore({RUN_VALUE_NAME: '"C:\\Old Path\\removed.exe"'})
    registration = StartupRegistration(store)

    disabled = registration.reconcile(enabled=False, command='"C:\\Current\\app.exe"')
    assert disabled.state is StartupState.disabled
    assert disabled.changed
    assert store.deletes == [RUN_VALUE_NAME]
    assert not store.values


def test_startup_reconciles_path_changes_and_is_idempotent() -> None:
    store = _MemoryRunStore()
    registration = StartupRegistration(store)
    current = '"C:\\New Path\\MeguminCompanion.exe"'

    first = registration.reconcile(enabled=True, command=current)
    repeat = registration.reconcile(enabled=True, command=current)
    assert first.state is StartupState.enabled and first.changed
    assert repeat.state is StartupState.enabled and not repeat.changed
    assert store.writes == [(RUN_VALUE_NAME, current)]
    assert registration.remove_for_uninstall()
    assert store.deletes == [RUN_VALUE_NAME]


def test_startup_noops_when_already_disabled_and_portable_adapter_stays_unavailable() -> None:
    store = _MemoryRunStore()
    registration = StartupRegistration(store)
    disabled = registration.reconcile(enabled=False, command='"C:\\Current\\app.exe"')
    assert disabled.state is StartupState.disabled
    assert not disabled.changed
    assert not registration.remove_for_uninstall()

    portable = UnsupportedStartupRegistration()
    result = portable.reconcile(enabled=True, command="desktop")
    assert result.state is StartupState.unavailable
    assert not result.changed
    assert not portable.remove_for_uninstall()


def test_startup_command_is_cwd_independent_for_frozen_and_wheel_modes(tmp_path: Path) -> None:
    executable = tmp_path / "folder with spaces" / "Megumin Companion.exe"
    frozen = desktop_startup_command(executable=executable, frozen=True)
    wheel = desktop_startup_command(executable=executable, frozen=False)

    assert str(executable.resolve()) in frozen
    assert "-m desktop_client" in wheel
    assert "schtasks" not in frozen.casefold()
    assert "schtasks" not in wheel.casefold()
