from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from app.config import (
    Settings,
    load_settings,
    patch_user_settings,
    read_user_settings,
)
from app.config.settings import LLMConfig, LoggingConfig, MemoryConfig, StorageConfig
from app.paths import AppPaths
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    DeepSeekFlashConfigureCommand,
    DeepSeekFlashDisableCommand,
    DesktopSettingsForm,
    ManagementCommand,
    ManagementResultEvent,
    SettingsSaveCommand,
    SettingsSnapshot,
    SettingsSnapshotEvent,
)
from desktop_client.ui.management import DesktopManagementRuntime, ManagementViewModel
from desktop_client.ui.settings_dialog import SettingsDialog
from PySide6.QtWidgets import QApplication


class _Secrets:
    def __init__(self) -> None:
        self.llm: str | None = None
        self.deepseek_flash: str | None = None
        self.vts: str | None = None

    def status(self, _settings: Settings) -> tuple[bool, bool]:
        return self.llm is not None, self.vts is not None

    def store_llm(self, _settings: Settings, value: str) -> None:
        self.llm = value

    def deepseek_flash_configured(self, _settings: Settings) -> bool:
        return self.deepseek_flash is not None

    def store_deepseek_flash(self, _settings: Settings, value: str) -> None:
        self.deepseek_flash = value

    async def store_vts(self, _settings: Settings, value: str) -> None:
        self.vts = value

    def revoke_llm(self, _settings: Settings) -> bool:
        changed = self.llm is not None
        self.llm = None
        return changed

    def revoke_deepseek_flash(self, _settings: Settings) -> bool:
        changed = self.deepseek_flash is not None
        self.deepseek_flash = None
        return changed

    def revoke_vts(self, _settings: Settings) -> bool:
        changed = self.vts is not None
        self.vts = None
        return changed


def _settings(root: Path, *, candidate_analysis_enabled: bool = False) -> Settings:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=True, database_path=Path("companion.sqlite3")),
        llm=LLMConfig(provider="mock"),
        memory=MemoryConfig(candidate_analysis_enabled=candidate_analysis_enabled),
    )
    settings._paths = AppPaths(root=root)
    return settings


def _form() -> DesktopSettingsForm:
    return DesktopSettingsForm(
        llm_provider="mock",
        llm_base_url="https://generic.example.invalid",
        llm_model="generic-model",
        tts_provider="mock",
        tts_base_url="http://127.0.0.1:9880",
        vts_enabled=False,
        vts_uri="ws://127.0.0.1:8001",
        vts_plugin_name="Megumin Companion",
        vts_plugin_developer="Local User",
        stt_enabled=False,
        stt_executable="whisper/whisper-cli.exe",
        stt_model_path="whisper/ggml-base.bin",
        stt_device="",
        startup_enabled=False,
    )


def _events(bridge: ApplicationBridge) -> list[object]:
    return list(bridge.drain_events())


def _result(events: list[object], operation: str) -> ManagementResultEvent:
    return next(
        event
        for event in events
        if isinstance(event, ManagementResultEvent) and event.operation == operation
    )


def test_deepseek_flash_management_keeps_generic_configuration_isolated(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        patch_user_settings(
            {
                "llm": {
                    "base_url": "https://generic.example.invalid",
                    "model": "generic-model",
                }
            },
            app_paths=settings.paths,
        )
        settings = load_settings(app_paths=settings.paths, environ={})
        secrets = _Secrets()
        management = DesktopManagementRuntime(settings, None, secrets=secrets)
        bridge = ApplicationBridge()
        capabilities = BackendCapabilities(text_chat=True, turn_cancel=True)
        secret = "deepseek-key-must-not-appear"

        await management.dispatch(
            bridge,
            DeepSeekFlashConfigureCommand(value=secret),
            capabilities=capabilities,
        )
        configured = _events(bridge)
        snapshot = next(event for event in configured if isinstance(event, SettingsSnapshotEvent))
        assert snapshot.snapshot.form.llm_provider == "deepseek"
        assert snapshot.snapshot.deepseek_flash_configured
        assert not snapshot.snapshot.llm_secret_configured
        assert _result(configured, "deepseek_flash_configured").restart_required
        assert secret not in repr(configured)
        assert secret not in settings.paths.settings.read_text(encoding="utf-8")
        assert secrets.deepseek_flash == secret

        await management.dispatch(
            bridge,
            SettingsSaveCommand(payload=replace(snapshot.snapshot.form, startup_enabled=True)),
            capabilities=capabilities,
        )
        saved = _events(bridge)
        assert _result(saved, "settings_saved").restart_required
        layer = read_user_settings(app_paths=settings.paths)
        assert layer["llm"]["provider"] == "deepseek"
        assert layer["llm"]["base_url"] == "https://generic.example.invalid"
        assert layer["llm"]["model"] == "generic-model"
        assert layer["desktop"]["startup_enabled"] is True

        await management.dispatch(
            bridge,
            DeepSeekFlashDisableCommand(),
            capabilities=capabilities,
        )
        disabled = _events(bridge)
        assert _result(disabled, "deepseek_flash_disabled").restart_required
        disabled_snapshot = next(
            event for event in disabled if isinstance(event, SettingsSnapshotEvent)
        )
        assert disabled_snapshot.snapshot.form.llm_provider == "none"
        assert not disabled_snapshot.snapshot.deepseek_flash_configured
        assert secrets.deepseek_flash is None

    asyncio.run(scenario())


def test_deepseek_flash_rejects_candidate_analysis_before_writing_a_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion", candidate_analysis_enabled=True)
        secrets = _Secrets()
        management = DesktopManagementRuntime(settings, None, secrets=secrets)
        bridge = ApplicationBridge()
        secret = "candidate-analysis-must-not-store"

        await management.dispatch(
            bridge,
            DeepSeekFlashConfigureCommand(value=secret),
            capabilities=BackendCapabilities(),
        )

        events = _events(bridge)
        result = _result(events, "deepseek_flash_configure")
        assert result.reason_code == "deepseek_memory_pro_required"
        assert secrets.deepseek_flash is None
        assert not settings.paths.settings.exists()
        assert secret not in repr(events)

    asyncio.run(scenario())


def test_deepseek_flash_activation_rolls_back_provider_when_key_write_fails(tmp_path: Path) -> None:
    class _WriteFailureSecrets(_Secrets):
        def store_deepseek_flash(self, _settings: Settings, _value: str) -> None:
            raise OSError("synthetic DPAPI write failure")

    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        secrets = _WriteFailureSecrets()
        management = DesktopManagementRuntime(settings, None, secrets=secrets)
        bridge = ApplicationBridge()

        await management.dispatch(
            bridge,
            DeepSeekFlashConfigureCommand(value="rollback-key-must-not-leak"),
            capabilities=BackendCapabilities(),
        )

        events = _events(bridge)
        result = _result(events, "deepseek_flash_configure")
        assert result.reason_code == "management_failed"
        assert management._settings.llm.provider == "mock"  # noqa: SLF001 - rollback contract
        assert load_settings(app_paths=settings.paths, environ={}).llm.provider == "mock"
        assert secrets.deepseek_flash is None
        assert "rollback-key-must-not-leak" not in repr(events)

    asyncio.run(scenario())


def test_deepseek_flash_activation_revokes_a_new_key_after_partial_write_failure(
    tmp_path: Path,
) -> None:
    class _PartialWriteFailureSecrets(_Secrets):
        def store_deepseek_flash(self, _settings: Settings, value: str) -> None:
            self.deepseek_flash = value
            raise OSError("synthetic post-replace verification failure")

    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        secrets = _PartialWriteFailureSecrets()
        management = DesktopManagementRuntime(settings, None, secrets=secrets)
        bridge = ApplicationBridge()
        secret = "partial-write-key-must-not-survive"

        await management.dispatch(
            bridge,
            DeepSeekFlashConfigureCommand(value=secret),
            capabilities=BackendCapabilities(),
        )

        events = _events(bridge)
        result = _result(events, "deepseek_flash_configure")
        assert result.reason_code == "management_failed"
        assert management._settings.llm.provider == "mock"  # noqa: SLF001 - rollback contract
        assert load_settings(app_paths=settings.paths, environ={}).llm.provider == "mock"
        assert secrets.deepseek_flash is None
        assert secret not in repr(events)

    asyncio.run(scenario())


def test_deepseek_flash_activation_rolls_back_before_key_write_when_reload_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        secrets = _Secrets()
        management = DesktopManagementRuntime(settings, None, secrets=secrets)
        bridge = ApplicationBridge()
        calls = 0

        def fail_once_then_load(*, app_paths: AppPaths, environ: dict[str, str]) -> Settings:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("synthetic settings reload failure")
            return load_settings(app_paths=app_paths, environ=environ)

        monkeypatch.setattr("desktop_client.ui.management.load_settings", fail_once_then_load)
        await management.dispatch(
            bridge,
            DeepSeekFlashConfigureCommand(value="reload-failure-key-must-not-leak"),
            capabilities=BackendCapabilities(),
        )

        events = _events(bridge)
        result = _result(events, "deepseek_flash_configure")
        assert result.reason_code == "management_failed"
        assert calls == 2
        assert management._settings.llm.provider == "mock"  # noqa: SLF001 - rollback contract
        assert load_settings(app_paths=settings.paths, environ={}).llm.provider == "mock"
        assert secrets.deepseek_flash is None
        assert "reload-failure-key-must-not-leak" not in repr(events)

    asyncio.run(scenario())


def test_deepseek_flash_disable_keeps_provider_off_if_key_revocation_fails(tmp_path: Path) -> None:
    class _RevokeFailureSecrets(_Secrets):
        def revoke_deepseek_flash(self, _settings: Settings) -> bool:
            raise OSError("synthetic DPAPI revoke failure")

    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        settings.llm = settings.llm.model_copy(update={"provider": "deepseek"})
        secrets = _RevokeFailureSecrets()
        secrets.deepseek_flash = "stale-key"
        management = DesktopManagementRuntime(settings, None, secrets=secrets)
        bridge = ApplicationBridge()

        await management.dispatch(
            bridge,
            DeepSeekFlashDisableCommand(),
            capabilities=BackendCapabilities(),
        )

        events = _events(bridge)
        result = _result(events, "deepseek_flash_disabled")
        assert result.reason_code == "deepseek_flash_key_revoke_failed"
        assert result.restart_required
        assert management._settings.llm.provider == "none"  # noqa: SLF001 - disable-first contract
        assert load_settings(app_paths=settings.paths, environ={}).llm.provider == "none"
        assert secrets.deepseek_flash == "stale-key"

    asyncio.run(scenario())


class _ConfirmingSettingsDialog(SettingsDialog):
    def __init__(
        self,
        model: ManagementViewModel,
        submit_command: Callable[[ManagementCommand], bool],
        *,
        confirmations: list[bool],
    ) -> None:
        self._confirmations = confirmations
        super().__init__(model, submit_command)

    def _confirm(self, _title: str, _message: str) -> bool:
        return self._confirmations.pop(0)


def test_deepseek_flash_settings_card_uses_separate_secret_and_locks_generic_controls(
    qapp: QApplication,
) -> None:
    del qapp
    model = ManagementViewModel()
    model.settings = SettingsSnapshot(
        form=_form(),
        llm_secret_configured=False,
        vts_secret_configured=False,
        settings_schema_upgrade_required=False,
    )
    submitted: list[ManagementCommand] = []

    def submit(command: ManagementCommand) -> bool:
        submitted.append(command)
        return True

    dialog = _ConfirmingSettingsDialog(model, submit, confirmations=[True, True, True])
    assert "远端处理、保留与地域风险不能由本项目消除" in dialog.deepseek_flash_guidance.text()
    dialog.deepseek_flash_secret.setText("dialog-deepseek-key")
    dialog._configure_deepseek_flash()
    configure = submitted.pop()
    assert isinstance(configure, DeepSeekFlashConfigureCommand)
    assert "dialog-deepseek-key" not in repr(configure)
    assert dialog.deepseek_flash_secret.text() == "dialog-deepseek-key"
    assert not dialog.isEnabled()
    model.apply_event(
        ManagementResultEvent(
            operation="deepseek_flash_configured",
            command_id=configure.command_id,
            restart_required=True,
        )
    )
    dialog.sync_from_model()
    assert dialog.deepseek_flash_secret.text() == ""
    assert dialog.isEnabled()
    assert dialog.llm_secret.isReadOnly() is False

    dialog.deepseek_flash_secret.setText("retry-deepseek-key")
    dialog._configure_deepseek_flash()
    failed_configure = submitted.pop()
    assert isinstance(failed_configure, DeepSeekFlashConfigureCommand)
    model.apply_event(
        ManagementResultEvent(
            operation="deepseek_flash_configure",
            command_id=failed_configure.command_id,
            reason_code="secret_io_failed",
        )
    )
    dialog.sync_from_model()
    assert dialog.deepseek_flash_secret.text() == "retry-deepseek-key"
    assert dialog.isEnabled()
    dialog.deepseek_flash_secret.clear()

    model.settings = SettingsSnapshot(
        form=replace(_form(), llm_provider="deepseek"),
        llm_secret_configured=False,
        vts_secret_configured=False,
        settings_schema_upgrade_required=False,
        deepseek_flash_configured=True,
    )
    dialog.sync_from_model()
    assert dialog.llm_provider.isReadOnly()
    assert dialog.llm_base_url.isReadOnly()
    assert dialog.llm_model.isReadOnly()
    assert not dialog.save_llm_secret.isEnabled()
    assert "已启用" in dialog.deepseek_flash_state.text()

    dialog._disable_deepseek_flash()
    assert isinstance(submitted.pop(), DeepSeekFlashDisableCommand)
    dialog.clear_sensitive()


def test_deepseek_flash_pending_input_waits_for_terminal_result_after_status_failure(
    qapp: QApplication,
) -> None:
    del qapp
    model = ManagementViewModel()
    model.settings = SettingsSnapshot(
        form=_form(),
        llm_secret_configured=False,
        vts_secret_configured=False,
        settings_schema_upgrade_required=False,
    )
    submitted: list[ManagementCommand] = []

    def submit(command: ManagementCommand) -> bool:
        submitted.append(command)
        return True

    dialog = _ConfirmingSettingsDialog(model, submit, confirmations=[True])
    dialog.deepseek_flash_secret.setText("retry-only-after-terminal-result")
    dialog._configure_deepseek_flash()
    command = submitted.pop()
    assert isinstance(command, DeepSeekFlashConfigureCommand)
    assert not dialog.isEnabled()

    model.apply_event(
        ManagementResultEvent(
            operation="deepseek_flash_status",
            command_id=command.command_id,
            reason_code="management_failed",
        )
    )
    dialog.sync_from_model()
    assert dialog.deepseek_flash_secret.text() == "retry-only-after-terminal-result"
    assert not dialog.isEnabled()

    model.apply_event(
        ManagementResultEvent(
            operation="deepseek_flash_configured",
            command_id=command.command_id,
            restart_required=True,
        )
    )
    dialog.sync_from_model()
    assert dialog.deepseek_flash_secret.text() == ""
    assert dialog.isEnabled()
