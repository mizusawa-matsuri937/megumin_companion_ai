from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import desktop_client.ui.management as management_module
import pytest
from app import stt_runtime as stt_runtime_module
from app.clients.vts import VTSToken
from app.config import ConfigurationError, Settings, read_user_settings, write_user_settings
from app.config.settings import LLMConfig, LoggingConfig, StorageConfig
from app.memory.models import (
    MemoryClaim,
    MemoryItem,
    MemorySensitivity,
    MemorySourceKind,
    MemoryStatus,
    MemoryType,
    SourceInputMode,
    SourceProvenance,
)
from app.memory.runtime import MemoryRuntime, create_memory_runtime
from app.memory.service import (
    ConfirmationNotFoundError,
    CredentialRejectedError,
    FeatureDisabledError,
)
from app.paths import AppPaths
from app.schemas import FeatureActualState, FeatureName, FeatureState
from app.secret_store import SecretStoreError, SecretStoreErrorCode
from app.stt_runtime import (
    ManagedChineseSttRuntime,
    SttRuntimeError,
    SttRuntimeInstallResult,
    SttRuntimeState,
    SttRuntimeStatus,
)
from desktop_client.ui.backend import BackendThreadHost, SkeletonBackendRuntime
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    MAX_SECRET_CHARS,
    BackendCapabilities,
    BackendState,
    DesktopSettingsForm,
    FeatureSetCommand,
    FeatureStatesEvent,
    ManagementCommand,
    ManagementDebugCommand,
    ManagementDebugEvent,
    ManagementRefreshCommand,
    ManagementResultEvent,
    MemoryClearCommand,
    MemoryConfirmationsEvent,
    MemoryConfirmationSummary,
    MemoryConfirmCommand,
    MemoryDeleteCommand,
    MemoryDetailCommand,
    MemoryDetailEvent,
    MemoryExportCommand,
    MemoryListCommand,
    MemoryListEvent,
    MemorySummary,
    MemoryUpdateCommand,
    SecretRevokeCommand,
    SecretStoreCommand,
    SettingsSaveCommand,
    SettingsSnapshot,
    SettingsSnapshotEvent,
    SttInstallCommand,
)
from desktop_client.ui.management import (
    DesktopManagementRuntime,
    DesktopSecretStore,
    DPAPIDesktopSecretStore,
    ManagementViewModel,
    _management_error_code,
    _ManagementFailure,
    _preview,
    _settings_patch,
    _write_memory_export,
)
from desktop_client.ui.settings_dialog import SettingsDialog
from desktop_client.ui.window import MainWindow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox


class _Secrets(DesktopSecretStore):
    def __init__(self) -> None:
        self.llm: str | None = None
        self.vts: str | None = None

    def status(self, _settings: Settings) -> tuple[bool, bool]:
        return self.llm is not None, self.vts is not None

    def store_llm(self, _settings: Settings, value: str) -> None:
        self.llm = value

    async def store_vts(self, _settings: Settings, value: str) -> None:
        self.vts = value

    def revoke_llm(self, _settings: Settings) -> bool:
        changed = self.llm is not None
        self.llm = None
        return changed

    def revoke_vts(self, _settings: Settings) -> bool:
        changed = self.vts is not None
        self.vts = None
        return changed


class _SttRuntime:
    def __init__(self, failure_code: str | None = None) -> None:
        self.failure_code = failure_code
        self.install_calls = 0
        self.state = SttRuntimeState.missing

    def status_for_settings(self, _settings: Settings) -> SttRuntimeStatus:
        return SttRuntimeStatus(self.state)

    async def install(self) -> SttRuntimeInstallResult:
        self.install_calls += 1
        if self.failure_code is not None:
            raise SttRuntimeError(self.failure_code)
        self.state = SttRuntimeState.verified
        return SttRuntimeInstallResult(changed=True, status=SttRuntimeStatus(self.state))


def _settings(root: Path) -> Settings:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=True, database_path=Path("companion.sqlite3")),
        llm=LLMConfig(provider="mock"),
    )
    settings._paths = AppPaths(root=root)
    return settings


def _seed_memory(runtime: MemoryRuntime) -> str:
    source = "我喜欢合成咖啡"
    result = runtime.memory.consider_user_claim(
        MemoryClaim(
            memory_type=MemoryType.fact,
            canonical_key="preference:synthetic-coffee",
            content="用户喜欢合成咖啡",
            evidence_quote=source,
            importance_score=0.9,
            confidence_score=0.95,
            sensitivity_hint=MemorySensitivity.normal,
        ),
        user_id="local_user",
        source_message_id="synthetic-seed",
        source_input_mode=SourceInputMode.text,
        source_text=source,
        created_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    assert result.item is not None
    return result.item.memory_id


def _seed_confirmation(runtime: MemoryRuntime) -> str:
    source = "synthetic sensitive preference evidence"
    result = runtime.memory.consider_user_claim(
        MemoryClaim(
            memory_type=MemoryType.fact,
            canonical_key="preference:sensitive-synthetic",
            content="synthetic sensitive preference",
            evidence_quote=source,
            importance_score=0.9,
            confidence_score=0.95,
            sensitivity_hint=MemorySensitivity.other_sensitive,
        ),
        user_id="local_user",
        source_message_id="synthetic-confirmation",
        source_input_mode=SourceInputMode.text,
        source_text=source,
        created_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    assert result.confirmation is not None
    return result.confirmation.confirmation_id


async def _runtime(root: Path) -> MemoryRuntime:
    runtime = await create_memory_runtime(str(root / "state" / "companion.sqlite3"))
    assert isinstance(runtime, MemoryRuntime)
    return runtime


def _form() -> DesktopSettingsForm:
    return DesktopSettingsForm(
        llm_provider="mock",
        llm_base_url="https://api.example.invalid",
        llm_model="",
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


def _memory_item(memory_id: str = "mem_ui") -> MemoryItem:
    timestamp = datetime(2026, 7, 21, 12, tzinfo=UTC)
    return MemoryItem(
        memory_id=memory_id,
        user_id="local_user",
        memory_type=MemoryType.fact,
        canonical_key="preference:ui-synthetic",
        content="synthetic UI memory",
        normalized_content="synthetic ui memory",
        importance_score=0.9,
        confidence_score=0.95,
        sensitivity=MemorySensitivity.normal,
        status=MemoryStatus.active,
        source_kind=MemorySourceKind.manual,
        source_message_id="manual-ui-synthetic",
        evidence_quote="synthetic evidence",
        source_sha256="0" * 64,
        provenance=SourceProvenance.manual,
        created_at=timestamp,
        updated_at=timestamp,
        last_seen_at=timestamp,
    )


def _events(bridge: ApplicationBridge) -> list[object]:
    return list(bridge.drain_events())


def test_explicit_stt_install_updates_only_managed_paths_and_publishes_status(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        write_user_settings(
            {
                "stt": {
                    "enabled": True,
                    "device": "preserve-device",
                    "threads": 3,
                    "executable": "manual/whisper-cli.exe",
                    "model_path": "manual/model.bin",
                    "language": "zh",
                }
            },
            app_paths=settings.paths,
        )
        stt_runtime = _SttRuntime()
        management = DesktopManagementRuntime(
            settings,
            None,
            secrets=_Secrets(),
            stt_runtime=cast(ManagedChineseSttRuntime, stt_runtime),
        )
        bridge = ApplicationBridge()
        await management.dispatch(
            bridge,
            SttInstallCommand(command_id="stt-install"),
            capabilities=BackendCapabilities(),
        )
        events = _events(bridge)
        assert stt_runtime.install_calls == 1
        snapshot = next(event for event in events if isinstance(event, SettingsSnapshotEvent))
        assert snapshot.snapshot.stt_runtime.state is SttRuntimeState.verified
        assert any(
            isinstance(event, ManagementResultEvent)
            and event.operation == "stt_runtime_installed"
            and event.restart_required
            for event in events
        )
        layer = read_user_settings(app_paths=settings.paths)
        assert layer["stt"] == {
            "enabled": True,
            "device": "preserve-device",
            "threads": 3,
            "managed_profile": "whispercpp_base_q5_1",
            "provider": "whisper_cpp",
            "executable": "stt/whispercpp/v1.9.1/bin/whisper-cli.exe",
            "model_path": "stt/whispercpp/v1.9.1/model/ggml-base-q5_1.bin",
            "language": "zh",
        }

    asyncio.run(scenario())


def test_stt_install_failure_reports_stable_code(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        management = DesktopManagementRuntime(
            settings,
            None,
            secrets=_Secrets(),
            stt_runtime=cast(ManagedChineseSttRuntime, _SttRuntime("stt_download_failed")),
        )
        bridge = ApplicationBridge()
        await management.dispatch(
            bridge,
            SttInstallCommand(command_id="stt-install-failed"),
            capabilities=BackendCapabilities(),
        )
        assert any(
            isinstance(event, ManagementResultEvent)
            and event.operation == "stt_runtime_install"
            and event.reason_code == "stt_download_failed"
            for event in _events(bridge)
        )

    asyncio.run(scenario())


def test_w16_settings_secret_and_feature_disable_wait_for_barrier(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        runtime = await _runtime(settings.paths.root)
        secrets = _Secrets()
        management = DesktopManagementRuntime(settings, runtime, secrets=secrets)
        bridge = ApplicationBridge()
        try:
            await management.publish_initial(
                bridge,
                capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
            )
            initial = _events(bridge)
            snapshot = next(event for event in initial if isinstance(event, SettingsSnapshotEvent))
            assert not snapshot.snapshot.llm_secret_configured

            secret = "w16-secret-must-not-be-echoed"
            command = SecretStoreCommand(secret_id="llm", value=secret)
            await management.dispatch(
                bridge,
                command,
                capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
            )
            secret_events = _events(bridge)
            assert secrets.llm == secret
            assert secret not in repr(secret_events)
            assert any(
                isinstance(event, ManagementResultEvent) and event.operation == "secret_stored"
                for event in secret_events
            )

            form = replace(snapshot.snapshot.form, stt_device="3", startup_enabled=True)
            await management.dispatch(
                bridge,
                SettingsSaveCommand(payload=form),
                capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
            )
            save_events = _events(bridge)
            assert any(
                isinstance(event, ManagementResultEvent)
                and event.operation == "settings_saved"
                and event.restart_required
                for event in save_events
            )
            layer = read_user_settings(app_paths=settings.paths)
            assert layer["stt"]["device"] == 3
            assert layer["desktop"]["startup_enabled"] is True
            assert "w16-secret-must-not-be-echoed" not in settings.paths.settings.read_text(
                encoding="utf-8"
            )

            await management.dispatch(
                bridge,
                SettingsSaveCommand(payload=replace(form, llm_provider="openai", llm_model="")),
                capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
            )
            missing_model = _events(bridge)
            assert any(
                isinstance(event, ManagementResultEvent)
                and event.operation == "settings_save"
                and event.reason_code == "llm_model_required"
                for event in missing_model
            )

            await management.dispatch(
                bridge,
                SettingsSaveCommand(payload=replace(form, tts_provider="gpt-sovits")),
                capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
            )
            missing_reference = _events(bridge)
            assert any(
                isinstance(event, ManagementResultEvent)
                and event.operation == "settings_save"
                and event.reason_code == "tts_reference_required"
                for event in missing_reference
            )

            entered = asyncio.Event()
            release = asyncio.Event()

            async def block_disable(state: object) -> None:
                if getattr(state, "name", None) is FeatureName.vision and not getattr(
                    state, "desired_enabled", True
                ):
                    entered.set()
                    await release.wait()

            runtime.add_feature_transition_handler(block_disable)
            await runtime.set_feature(FeatureName.vision, True)
            task = asyncio.create_task(
                management.dispatch(
                    bridge,
                    FeatureSetCommand(feature=FeatureName.vision, enabled=False),
                    capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert not task.done()
            transition_events = _events(bridge)
            transition = next(
                event for event in transition_events if isinstance(event, FeatureStatesEvent)
            )
            transitioning_vision = next(
                state for state in transition.states if state.name is FeatureName.vision
            )
            assert transitioning_vision.desired_enabled is False
            assert transitioning_vision.actual_state is FeatureActualState.disabling
            release.set()
            await task
            feature_events = _events(bridge)
            states = next(
                event for event in feature_events if isinstance(event, FeatureStatesEvent)
            )
            vision = next(state for state in states.states if state.name is FeatureName.vision)
            assert vision.actual_state is FeatureActualState.disabled
        finally:
            await runtime.close()

    asyncio.run(scenario())
    qapp.processEvents()


def test_w16_memory_list_detail_update_delete_and_export_are_bounded(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        runtime = await _runtime(settings.paths.root)
        management = DesktopManagementRuntime(settings, runtime, secrets=_Secrets())
        bridge = ApplicationBridge()
        try:
            await runtime.set_feature(FeatureName.long_term_memory, True)
            memory_id = _seed_memory(runtime)
            capabilities = BackendCapabilities(text_chat=True, turn_cancel=True)

            await management.dispatch(bridge, MemoryListCommand(), capabilities=capabilities)
            listed = _events(bridge)
            listing = next(event for event in listed if isinstance(event, MemoryListEvent))
            assert len(listing.items) == 1
            assert listing.items[0].memory_id == memory_id
            assert listing.items[0].content_preview == "用户喜欢合成咖啡"

            await management.dispatch(
                bridge,
                MemoryDetailCommand(memory_id=memory_id),
                capabilities=capabilities,
            )
            details = _events(bridge)
            detail = next(event for event in details if isinstance(event, MemoryDetailEvent))
            assert detail.item is not None
            assert detail.item.content == "用户喜欢合成咖啡"

            await management.dispatch(
                bridge,
                MemoryUpdateCommand(memory_id=memory_id, content="用户改为喜欢合成茶"),
                capabilities=capabilities,
            )
            updated = _events(bridge)
            assert any(
                isinstance(event, ManagementResultEvent) and event.operation == "memory_updated"
                for event in updated
            )

            destination = tmp_path / "explicit-export.json"
            await management.dispatch(
                bridge,
                MemoryExportCommand(destination=str(destination)),
                capabilities=capabilities,
            )
            exported = _events(bridge)
            assert any(
                isinstance(event, ManagementResultEvent) and event.operation == "memory_exported"
                for event in exported
            )
            payload = json.loads(destination.read_text(encoding="utf-8"))
            assert [item["content"] for item in payload["memories"]] == ["用户改为喜欢合成茶"]

            await management.dispatch(
                bridge,
                MemoryDeleteCommand(memory_id=memory_id),
                capabilities=capabilities,
            )
            deleted = _events(bridge)
            result = next(
                event
                for event in deleted
                if isinstance(event, ManagementResultEvent) and event.operation == "memory_deleted"
            )
            assert result.deleted_count == 1
            assert result.cleanup_pending
        finally:
            await runtime.close()

    asyncio.run(scenario())
    qapp.processEvents()


def test_w16_management_refresh_secret_revoke_clear_confirm_and_debug(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        runtime = await _runtime(settings.paths.root)
        secrets = _Secrets()
        management = DesktopManagementRuntime(settings, runtime, secrets=secrets)
        bridge = ApplicationBridge()
        capabilities = BackendCapabilities(text_chat=True, turn_cancel=True)
        try:
            assert DesktopManagementRuntime.handles(ManagementRefreshCommand())
            assert not DesktopManagementRuntime.handles(object())
            await runtime.set_feature(FeatureName.long_term_memory, True)
            memory_id = _seed_memory(runtime)
            confirmation_id = _seed_confirmation(runtime)

            await management.dispatch(
                bridge,
                ManagementRefreshCommand(),
                capabilities=capabilities,
            )
            refresh_events = _events(bridge)
            assert any(isinstance(event, SettingsSnapshotEvent) for event in refresh_events)
            assert any(isinstance(event, FeatureStatesEvent) for event in refresh_events)
            assert any(isinstance(event, MemoryListEvent) for event in refresh_events)
            assert any(isinstance(event, MemoryConfirmationsEvent) for event in refresh_events)
            assert any(isinstance(event, ManagementDebugEvent) for event in refresh_events)

            model = ManagementViewModel()
            assert all(model.apply_event(event) for event in refresh_events)
            assert model.settings is not None
            assert model.feature_states
            assert model.confirmations
            assert model.debug is not None

            await management.dispatch(
                bridge,
                SecretStoreCommand(secret_id="llm", value="synthetic-llm-key"),
                capabilities=capabilities,
            )
            await management.dispatch(
                bridge,
                SecretStoreCommand(secret_id="vts", value="synthetic-vts-token"),
                capabilities=capabilities,
            )
            stored = _events(bridge)
            assert secrets.llm is not None and secrets.vts is not None
            assert all("synthetic-llm-key" not in repr(event) for event in stored)

            await management.dispatch(
                bridge,
                SecretRevokeCommand(secret_id="llm"),
                capabilities=capabilities,
            )
            await management.dispatch(
                bridge,
                SecretRevokeCommand(secret_id="vts"),
                capabilities=capabilities,
            )
            revoked = _events(bridge)
            assert secrets.status(settings) == (False, False)
            assert (
                sum(
                    isinstance(event, ManagementResultEvent) and event.operation == "secret_revoked"
                    for event in revoked
                )
                == 2
            )

            await management.dispatch(
                bridge,
                MemoryListCommand(query="合成"),
                capabilities=capabilities,
            )
            searched = _events(bridge)
            listing = next(event for event in searched if isinstance(event, MemoryListEvent))
            assert listing.query == "合成"
            assert listing.items[0].memory_id == memory_id

            await management.dispatch(
                bridge,
                MemoryDetailCommand(memory_id=memory_id),
                capabilities=capabilities,
            )
            detail_events = _events(bridge)
            assert any(isinstance(event, MemoryDetailEvent) for event in detail_events)

            await management.dispatch(
                bridge,
                MemoryClearCommand(target="history"),
                capabilities=capabilities,
            )
            history_events = _events(bridge)
            assert any(
                isinstance(event, ManagementResultEvent) and event.operation == "history_cleared"
                for event in history_events
            )

            await management.dispatch(
                bridge,
                MemoryConfirmCommand(confirmation_id=confirmation_id, approved=True),
                capabilities=capabilities,
            )
            confirmed = _events(bridge)
            assert any(
                isinstance(event, ManagementResultEvent) and event.operation == "memory_confirmed"
                for event in confirmed
            )

            await management.dispatch(
                bridge,
                MemoryClearCommand(target="memories"),
                capabilities=capabilities,
            )
            cleared = _events(bridge)
            assert any(
                isinstance(event, ManagementResultEvent)
                and event.operation == "memories_cleared"
                and event.cleanup_pending
                for event in cleared
            )

            await management.dispatch(
                bridge,
                ManagementDebugCommand(),
                capabilities=capabilities,
            )
            debug_events = _events(bridge)
            assert any(isinstance(event, ManagementDebugEvent) for event in debug_events)
            model.clear_sensitive()
        finally:
            await runtime.close()

    asyncio.run(scenario())
    qapp.processEvents()


def test_w16_management_unavailable_operations_are_body_free(tmp_path: Path) -> None:
    async def scenario() -> None:
        management = DesktopManagementRuntime(_settings(tmp_path / "MeguminCompanion"), None)
        bridge = ApplicationBridge()
        capabilities = BackendCapabilities()
        commands: tuple[ManagementCommand, ...] = (
            ManagementRefreshCommand(),
            FeatureSetCommand(feature=FeatureName.vision, enabled=False),
            MemoryListCommand(),
            MemoryDetailCommand(memory_id="mem_missing"),
            MemoryUpdateCommand(memory_id="mem_missing", content="synthetic update"),
            MemoryDeleteCommand(memory_id="mem_missing"),
            MemoryClearCommand(target="history"),
            MemoryConfirmCommand(confirmation_id="confirm_missing", approved=False),
            MemoryExportCommand(destination=str(tmp_path / "export.json")),
            ManagementDebugCommand(),
        )
        for command in commands:
            await management.dispatch(bridge, command, capabilities=capabilities)
        events = _events(bridge)
        unavailable = [
            event
            for event in events
            if isinstance(event, ManagementResultEvent)
            and event.reason_code == "private_state_runtime_disabled"
        ]
        assert len(unavailable) >= 8
        assert "mem_missing" not in repr(unavailable)

    asyncio.run(scenario())


def test_w16_secret_store_adapter_and_export_writer_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SecretFile:
        def __init__(self) -> None:
            self.value: str | None = None

        @property
        def exists(self) -> bool:
            return self.value is not None

        def write_text(self, value: str) -> None:
            self.value = value

        def revoke(self) -> bool:
            changed = self.value is not None
            self.value = None
            return changed

    class _TokenStore:
        saved: list[VTSToken] = []

        def __init__(self, _secret_file: _SecretFile) -> None:
            pass

        async def save(self, token: VTSToken) -> None:
            self.saved.append(token)

    llm_file = _SecretFile()
    vts_file = _SecretFile()
    monkeypatch.setattr(management_module, "llm_api_key_file", lambda _paths: llm_file)
    monkeypatch.setattr(
        management_module,
        "vts_token_file",
        lambda _paths, _path: vts_file,
    )
    monkeypatch.setattr(management_module, "DPAPITokenStore", _TokenStore)
    settings = _settings(tmp_path / "MeguminCompanion")
    store = DPAPIDesktopSecretStore()
    assert store.status(settings) == (False, False)

    store.store_llm(settings, "synthetic-llm-key")
    asyncio.run(store.store_vts(settings, "synthetic-vts-token"))
    assert store.status(settings) == (True, False)
    assert llm_file.value == "synthetic-llm-key"
    assert len(_TokenStore.saved) == 1
    token = _TokenStore.saved[0]
    assert token.plugin_name == settings.vts.plugin_name
    assert token.plugin_developer == settings.vts.plugin_developer
    assert token.authentication_token == "synthetic-vts-token"
    assert store.revoke_llm(settings)
    assert not store.revoke_llm(settings)
    assert not store.revoke_vts(settings)

    payload = {"memories": [{"content": "synthetic export"}]}
    destination = tmp_path / "export.json"
    _write_memory_export(str(destination), payload, overwrite=False)
    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    with pytest.raises(_ManagementFailure) as existing:
        _write_memory_export(str(destination), payload, overwrite=False)
    assert existing.value.code == "export_destination_exists"
    with pytest.raises(_ManagementFailure) as invalid:
        _write_memory_export("relative.json", payload, overwrite=False)
    assert invalid.value.code == "export_path_invalid"
    with pytest.raises(_ManagementFailure) as missing_directory:
        _write_memory_export(
            str(tmp_path / "missing" / "export.json"),
            payload,
            overwrite=False,
        )
    assert missing_directory.value.code == "export_directory_unavailable"
    with pytest.raises(_ManagementFailure) as directory_target:
        _write_memory_export(str(tmp_path), payload, overwrite=False)
    assert directory_target.value.code == "export_destination_invalid"

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(_ManagementFailure) as write_failure:
        _write_memory_export(str(tmp_path / "write-failure.json"), payload, overwrite=False)
    assert write_failure.value.code == "export_write_failed"
    assert not list(tmp_path.glob(".*.part"))


def test_w16_management_helpers_cover_device_normalization_and_error_codes() -> None:
    form = DesktopSettingsForm(
        llm_provider="mock",
        llm_base_url="https://api.example.invalid",
        llm_model="",
        tts_provider="mock",
        tts_base_url="http://127.0.0.1:9880",
        vts_enabled=False,
        vts_uri="ws://127.0.0.1:8001",
        vts_plugin_name="Megumin Companion",
        vts_plugin_developer="Local User",
        stt_enabled=False,
        stt_executable="whisper/whisper-cli.exe",
        stt_model_path="whisper/ggml-base.bin",
        stt_device="device name",
        startup_enabled=False,
    )
    assert _settings_patch(replace(form, stt_device="")).get("stt") == {
        "enabled": False,
        "managed_profile": "whispercpp_base_q5_1",
        "executable": "whisper/whisper-cli.exe",
        "model_path": "whisper/ggml-base.bin",
        "device": None,
    }
    assert _settings_patch(replace(form, stt_device="7"))["stt"] == {
        "enabled": False,
        "managed_profile": "whispercpp_base_q5_1",
        "executable": "whisper/whisper-cli.exe",
        "model_path": "whisper/ggml-base.bin",
        "device": 7,
    }
    assert _settings_patch(form)["stt"] == {
        "enabled": False,
        "managed_profile": "whispercpp_base_q5_1",
        "executable": "whisper/whisper-cli.exe",
        "model_path": "whisper/ggml-base.bin",
        "device": "device name",
    }
    assert _preview("  synthetic   preview ") == "synthetic preview"
    assert len(_preview("x" * 600)) == 512
    assert (
        _management_error_code(_ManagementFailure("export_path_invalid")) == "export_path_invalid"
    )
    assert _management_error_code(ValueError("synthetic")) == "invalid_management_input"
    assert _management_error_code(RuntimeError("synthetic")) == "management_failed"
    assert (
        _management_error_code(SecretStoreError(SecretStoreErrorCode.io_failed, "synthetic-secret"))
        == "secret_io_failed"
    )
    assert (
        _management_error_code(CredentialRejectedError("synthetic credential"))
        == "credential_content_forbidden"
    )
    assert (
        _management_error_code(ConfirmationNotFoundError("synthetic confirmation"))
        == "confirmation_not_found"
    )
    assert (
        _management_error_code(FeatureDisabledError("synthetic feature"))
        == "long_term_memory_disabled"
    )
    assert (
        _management_error_code(ConfigurationError("synthetic configuration")) == "settings_invalid"
    )


def test_w16_management_reports_stable_error_codes_for_backend_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingSecrets(_Secrets):
        @staticmethod
        def _error() -> SecretStoreError:
            return SecretStoreError(SecretStoreErrorCode.io_failed, "synthetic-secret")

        def status(self, _settings: Settings) -> tuple[bool, bool]:
            raise self._error()

        def store_llm(self, _settings: Settings, _value: str) -> None:
            raise self._error()

        async def store_vts(self, _settings: Settings, _value: str) -> None:
            raise self._error()

        def revoke_llm(self, _settings: Settings) -> bool:
            raise self._error()

        def revoke_vts(self, _settings: Settings) -> bool:
            raise self._error()

    class _FailingMemoryRuntime:
        async def list_features(self) -> object:
            raise FeatureDisabledError("synthetic feature failure")

        async def set_feature(self, *_args: object, **_kwargs: object) -> object:
            raise FeatureDisabledError("synthetic feature failure")

        async def search_memories(self, **_kwargs: object) -> object:
            raise CredentialRejectedError("synthetic credential failure")

        async def list_memories(self, **_kwargs: object) -> object:
            raise ConfigurationError("synthetic memory configuration failure")

        async def get_memory(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("synthetic detail failure")

        async def update_memory(self, *_args: object, **_kwargs: object) -> object:
            raise ConfirmationNotFoundError("synthetic update failure")

        async def delete_memory(self, *_args: object, **_kwargs: object) -> object:
            raise FeatureDisabledError("synthetic delete failure")

        async def clear_history(self, **_kwargs: object) -> object:
            raise ConfigurationError("synthetic clear failure")

        async def clear_memories(self, **_kwargs: object) -> object:
            raise ConfigurationError("synthetic clear failure")

        async def pending_confirmations(self) -> object:
            raise RuntimeError("synthetic confirmation list failure")

        async def confirm_memory(self, *_args: object, **_kwargs: object) -> object:
            raise ConfirmationNotFoundError("synthetic confirmation failure")

        async def export(self, **_kwargs: object) -> object:
            raise _ManagementFailure("export_path_invalid")

    def result_for(events: list[object], operation: str) -> ManagementResultEvent:
        return next(
            event
            for event in events
            if isinstance(event, ManagementResultEvent) and event.operation == operation
        )

    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        capabilities = BackendCapabilities(text_chat=True, turn_cancel=True)
        bridge = ApplicationBridge()
        management = DesktopManagementRuntime(
            settings,
            cast(MemoryRuntime, _FailingMemoryRuntime()),
            secrets=_FailingSecrets(),
        )

        async def dispatch(command: ManagementCommand) -> list[object]:
            await management.dispatch(bridge, command, capabilities=capabilities)
            events = _events(bridge)
            assert "synthetic feature failure" not in repr(events)
            assert "synthetic credential failure" not in repr(events)
            return events

        settings_failure = await dispatch(
            SettingsSaveCommand(
                payload=replace(
                    _form(),
                    llm_provider="openai",
                    llm_model="synthetic-model",
                )
            )
        )
        assert result_for(settings_failure, "settings_save").reason_code == "secret_io_failed"

        store_failure = await dispatch(
            SecretStoreCommand(secret_id="llm", value="not-for-event-output")
        )
        assert result_for(store_failure, "secret_store").reason_code == "secret_io_failed"
        revoke_failure = await dispatch(SecretRevokeCommand(secret_id="vts"))
        assert result_for(revoke_failure, "secret_revoke").reason_code == "secret_io_failed"

        refreshed = await dispatch(ManagementRefreshCommand())
        assert {
            ("settings_status", "secret_io_failed"),
            ("feature_list", "long_term_memory_disabled"),
            ("memory_list", "settings_invalid"),
            ("memory_confirmations", "management_failed"),
        }.issubset(
            {
                (event.operation, event.reason_code)
                for event in refreshed
                if isinstance(event, ManagementResultEvent)
            }
        )

        feature_failure = await dispatch(
            FeatureSetCommand(feature=FeatureName.vision, enabled=True)
        )
        assert result_for(feature_failure, "feature_set").reason_code == "long_term_memory_disabled"
        list_failure = await dispatch(MemoryListCommand(query="synthetic search"))
        assert result_for(list_failure, "memory_list").reason_code == "credential_content_forbidden"
        detail_failure = await dispatch(MemoryDetailCommand(memory_id="mem_failure"))
        detail = next(event for event in detail_failure if isinstance(event, MemoryDetailEvent))
        assert detail.item is None
        assert detail.reason_code == "management_failed"
        update_failure = await dispatch(
            MemoryUpdateCommand(memory_id="mem_failure", content="synthetic update")
        )
        assert result_for(update_failure, "memory_update").reason_code == "confirmation_not_found"
        delete_failure = await dispatch(MemoryDeleteCommand(memory_id="mem_failure"))
        assert (
            result_for(delete_failure, "memory_delete").reason_code == "long_term_memory_disabled"
        )
        clear_failure = await dispatch(MemoryClearCommand(target="history"))
        assert result_for(clear_failure, "memory_clear").reason_code == "settings_invalid"
        confirmation_failure = await dispatch(
            MemoryConfirmCommand(confirmation_id="confirm_failure", approved=False)
        )
        assert (
            result_for(confirmation_failure, "memory_confirm").reason_code
            == "confirmation_not_found"
        )
        export_failure = await dispatch(
            MemoryExportCommand(destination=str(tmp_path / "failure-export.json"))
        )
        assert result_for(export_failure, "memory_export").reason_code == "export_path_invalid"

        healthy_bridge = ApplicationBridge()
        healthy_management = DesktopManagementRuntime(settings, None, secrets=_Secrets())

        async def dispatch_healthy(command: ManagementCommand) -> list[object]:
            await healthy_management.dispatch(healthy_bridge, command, capabilities=capabilities)
            return _events(healthy_bridge)

        secret_required = await dispatch_healthy(
            SettingsSaveCommand(
                payload=replace(
                    _form(),
                    llm_provider="openai",
                    llm_model="synthetic-model",
                )
            )
        )
        assert result_for(secret_required, "settings_save").reason_code == "secret_required"
        unsupported_tts = await dispatch_healthy(
            SettingsSaveCommand(payload=replace(_form(), tts_provider="unsupported"))
        )
        assert (
            result_for(unsupported_tts, "settings_save").reason_code == "tts_provider_unsupported"
        )

        def reject_patch(*_args: object, **_kwargs: object) -> None:
            raise ConfigurationError("synthetic patch failure")

        monkeypatch.setattr(management_module, "patch_user_settings", reject_patch)
        patch_failure = await dispatch_healthy(SettingsSaveCommand(payload=_form()))
        assert result_for(patch_failure, "settings_save").reason_code == "settings_invalid"

    asyncio.run(scenario())


def test_w16_settings_dialog_has_accessible_surface_and_wipes_on_final_close(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: SkeletonBackendRuntime(),
        auto_restart_limit=0,
    )
    window = MainWindow(bridge, host)
    form = DesktopSettingsForm(
        llm_provider="mock",
        llm_base_url="https://api.example.invalid",
        llm_model="",
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
    window.management_model.apply_event(
        SettingsSnapshotEvent(
            snapshot=SettingsSnapshot(
                form=form,
                llm_secret_configured=False,
                vts_secret_configured=False,
                settings_schema_upgrade_required=False,
            )
        )
    )
    window.model.connection_state = BackendState.ready
    window._sync_view()
    window.show()
    window.settings_button.click()
    qapp.processEvents()
    dialog = window._settings_dialog
    assert dialog is not None
    assert dialog.tabs.accessibleName() == "设置页面"
    assert dialog.llm_secret.echoMode() == dialog.llm_secret.EchoMode.Password
    assert "功能与隐私" in [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]

    window.management_model.apply_event(
        ManagementResultEvent(
            operation="settings_save",
            command_id="cmd_w16",
            reason_code="llm_model_required",
        )
    )
    dialog.sync_from_model()
    assert "模型名" in dialog.status_label.text()
    assert dialog.debug_error_code.text() == "llm_model_required"

    dialog.llm_secret.setText("dialog-secret-that-must-be-cleared")
    dialog.reject()
    assert dialog.llm_secret.text() == ""

    dialog.llm_secret.setText("dialog-secret-that-must-be-cleared")
    window.discard_sensitive_state()

    assert dialog.llm_secret.text() == ""
    assert window.management_model.settings is None
    assert window._settings_dialog is None
    window.close()


def test_stt_profile_selector_only_uses_registered_whisper_paths(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qapp
    larger = replace(
        stt_runtime_module.MANAGED_CHINESE_STT_MANIFEST,
        profile="whispercpp_small_q5_1",
        model_filename="ggml-small-q5_1.bin",
        display_name="Whisper small · Q5_1",
    )
    monkeypatch.setattr(
        stt_runtime_module,
        "MANAGED_WHISPER_STT_PROFILES",
        (stt_runtime_module.MANAGED_CHINESE_STT_MANIFEST, larger),
    )
    model = ManagementViewModel()
    model.settings = SettingsSnapshot(
        form=_form(),
        llm_secret_configured=False,
        vts_secret_configured=False,
        settings_schema_upgrade_required=False,
    )
    dialog = SettingsDialog(model, lambda _command: True)

    assert dialog.stt_profile.count() == 2
    dialog.stt_profile.setCurrentIndex(1)

    assert dialog.stt_profile.currentData() == larger.profile
    assert dialog.stt_executable.text() == (
        "stt/whispercpp/profiles/whispercpp_small_q5_1/v1.9.1/bin/whisper-cli.exe"
    )
    assert dialog.stt_model_path.text() == (
        "stt/whispercpp/profiles/whispercpp_small_q5_1/v1.9.1/model/ggml-small-q5_1.bin"
    )
    assert dialog._form_dirty
    dialog.clear_sensitive()


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


def test_stt_install_requires_saved_whisper_profile_selection(
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

    dialog = _ConfirmingSettingsDialog(
        model,
        submit,
        confirmations=[True],
    )

    dialog._form_dirty = True
    dialog._install_chinese_stt()
    assert submitted == []
    assert "保存设置" in dialog.status_label.text()

    dialog._form_dirty = False
    dialog._install_chinese_stt()
    assert len(submitted) == 1
    assert isinstance(submitted[0], SttInstallCommand)
    assert not dialog.install_stt_runtime.isEnabled()
    assert "安装中" in dialog.stt_runtime_status.text()
    dialog.clear_sensitive()


def test_w16_settings_dialog_renders_empty_and_validation_states(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ManagementViewModel()
    submitted: list[ManagementCommand] = []

    def submit(command: ManagementCommand) -> bool:
        submitted.append(command)
        return True

    dialog = _ConfirmingSettingsDialog(model, submit, confirmations=[False])
    dialog.sync_from_model()
    assert dialog._feature_buttons[FeatureName.vision].text() == "不可用"
    assert not dialog._feature_buttons[FeatureName.vision].isEnabled()

    dialog._save_settings()
    assert dialog.status_label.text() == "设置尚未加载。"
    dialog._toggle_feature(FeatureName.vision)
    dialog._load_selected_memory()
    dialog._save_memory()
    dialog._delete_memory()
    dialog._confirm_selected(False)
    assert dialog.status_label.text() == "请选择一条记忆建议。"

    dialog.memory_search.setText("x" * (MAX_SECRET_CHARS + 1))
    dialog._search_memory()
    assert dialog.status_label.text() == "搜索内容无效。"

    def reject_secret_command(_command: ManagementCommand) -> bool:
        raise ValueError("synthetic secret rejection")

    dialog._submit_command = reject_secret_command
    dialog.llm_secret.setText("synthetic-dialog-key")
    dialog._store_secret("llm")
    assert dialog.status_label.text() == "密钥或令牌无效。"
    dialog._submit_command = submit

    form = _form()
    model.apply_event(
        SettingsSnapshotEvent(
            snapshot=SettingsSnapshot(
                form=form,
                llm_secret_configured=True,
                vts_secret_configured=True,
                settings_schema_upgrade_required=False,
            )
        )
    )
    dialog.sync_from_model()
    assert dialog.llm_secret_state.text() == "LLM 密钥：已配置"
    assert dialog.vts_secret_state.text() == "VTS 令牌：已配置"

    dialog.llm_provider.setText("draft-provider")
    dialog._mark_form_dirty("draft-provider")
    model.apply_event(
        SettingsSnapshotEvent(
            snapshot=SettingsSnapshot(
                form=replace(form, llm_provider="fresh-provider"),
                llm_secret_configured=False,
                vts_secret_configured=False,
                settings_schema_upgrade_required=False,
            )
        )
    )
    dialog.sync_from_model()
    assert dialog.llm_provider.text() == "draft-provider"

    model.apply_event(MemoryListEvent(items=(), truncated=True))
    model.apply_event(
        MemoryDetailEvent(
            memory_id="mem_missing",
            item=None,
            reason_code="memory_not_found",
        )
    )
    model.apply_event(
        ManagementResultEvent(
            operation="settings_saved",
            command_id="cmd_ui_state",
            cleanup_pending=True,
            restart_required=True,
        )
    )
    dialog.sync_from_model()
    assert dialog.memory_list_notice.text() == "结果已截断为前 20 条。"
    assert dialog.memory_detail_label.text() == "记忆不可用：memory_not_found"
    assert "重启后生效" in dialog.status_label.text()
    assert "底层清理待处理" in dialog.status_label.text()
    assert not dialog._form_dirty

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(tmp_path / "cancelled-export.json"), ""),
    )
    dialog._export_memories()
    assert not any(isinstance(command, MemoryExportCommand) for command in submitted)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: ("", ""))
    dialog._export_memories()

    dialog.clear_sensitive()
    assert dialog.llm_secret.text() == ""
    assert dialog.memory_detail.toPlainText() == ""
    dialog.close()
    qapp.processEvents()


def test_w16_settings_dialog_submits_bounded_management_actions(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ManagementViewModel()
    form = _form()
    item = _memory_item()
    timestamp = datetime(2026, 7, 21, 12, tzinfo=UTC)
    model.apply_event(
        SettingsSnapshotEvent(
            snapshot=SettingsSnapshot(
                form=form,
                llm_secret_configured=False,
                vts_secret_configured=False,
                settings_schema_upgrade_required=False,
            )
        )
    )
    model.apply_event(
        FeatureStatesEvent(
            states=tuple(FeatureState(name=feature, enabled=False) for feature in FeatureName)
        )
    )
    model.apply_event(
        MemoryListEvent(
            items=(
                MemorySummary(
                    memory_id=item.memory_id,
                    memory_type=item.memory_type,
                    content_preview="synthetic UI memory",
                    sensitivity=item.sensitivity,
                    status=item.status,
                    updated_at=item.updated_at,
                ),
            )
        )
    )
    model.apply_event(MemoryDetailEvent(memory_id=item.memory_id, item=item))
    model.apply_event(
        MemoryConfirmationsEvent(
            items=(
                MemoryConfirmationSummary(
                    confirmation_id="confirm_ui",
                    content_preview="synthetic proposal",
                    evidence_preview="synthetic evidence",
                    expires_at=timestamp,
                ),
            )
        )
    )
    model.apply_event(
        ManagementDebugEvent(
            version="0.0-test",
            capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
            command_queue_count=0,
            command_queue_capacity=64,
            event_queue_count=0,
            event_queue_capacity=512,
        )
    )
    submitted: list[ManagementCommand] = []

    def submit(command: ManagementCommand) -> bool:
        submitted.append(command)
        return True

    dialog = _ConfirmingSettingsDialog(
        model,
        submit,
        confirmations=[True] * 10,
    )
    dialog.sync_from_model()
    assert dialog.memory_detail.toPlainText() == item.content
    assert dialog.debug_version.text() == "0.0-test"

    dialog._refresh()
    dialog._save_settings()
    dialog._store_secret("llm")
    assert "非空" in dialog.status_label.text()
    dialog.llm_secret.setText("synthetic-dialog-key")
    dialog._store_secret("llm")
    assert dialog.llm_secret.text() == ""
    dialog.vts_secret.setText("synthetic-dialog-token")
    dialog._store_secret("vts")
    dialog._revoke_secret("llm")
    dialog._toggle_feature(FeatureName.vision)
    dialog.memory_search.setText("synthetic")
    dialog._search_memory()
    selected_memory = dialog.memory_tree.topLevelItem(0)
    assert selected_memory is not None
    dialog.memory_tree.setCurrentItem(selected_memory)
    dialog._save_memory()
    dialog._delete_memory()
    dialog._clear_memory("history")
    dialog._clear_memory("memories")
    selected_confirmation = dialog.confirmation_tree.topLevelItem(0)
    assert selected_confirmation is not None
    dialog.confirmation_tree.setCurrentItem(selected_confirmation)
    dialog._confirm_selected(True)
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(tmp_path / "dialog-export.json"), ""),
    )
    dialog._export_memories()

    assert {
        ManagementRefreshCommand,
        SettingsSaveCommand,
        SecretStoreCommand,
        SecretRevokeCommand,
        FeatureSetCommand,
        MemoryListCommand,
        MemoryDetailCommand,
        MemoryUpdateCommand,
        MemoryDeleteCommand,
        MemoryClearCommand,
        MemoryConfirmCommand,
        MemoryExportCommand,
    }.issubset({type(command) for command in submitted})
    export = next(command for command in submitted if isinstance(command, MemoryExportCommand))
    assert export.overwrite

    model.apply_event(
        ManagementResultEvent(
            operation="settings_save",
            command_id="cmd_ui_failure",
            reason_code="llm_model_required",
        )
    )
    dialog.sync_from_model()
    assert "模型名" in dialog.status_label.text()
    assert dialog.debug_error_code.text() == "llm_model_required"
    dialog.clear_sensitive()
    assert dialog.memory_detail.toPlainText() == ""
    assert dialog.llm_secret.text() == ""
    dialog.close()
    qapp.processEvents()

    rejected = _ConfirmingSettingsDialog(model, lambda _command: False, confirmations=[True])
    rejected.sync_from_model()
    rejected._toggle_feature(FeatureName.vision)
    assert rejected._feature_buttons[FeatureName.vision].isEnabled()
    rejected.close()
    qapp.processEvents()


def test_w16_feature_enable_requires_a_second_confirmation(qapp: QApplication) -> None:
    model = ManagementViewModel()
    model.apply_event(
        FeatureStatesEvent(states=(FeatureState(name=FeatureName.cloud_vision, enabled=False),))
    )
    submitted: list[ManagementCommand] = []

    def submit(command: ManagementCommand) -> bool:
        submitted.append(command)
        return True

    dialog = _ConfirmingSettingsDialog(
        model,
        submit,
        confirmations=[False, True],
    )
    dialog.sync_from_model()

    dialog._toggle_feature(FeatureName.cloud_vision)
    assert submitted == []
    assert dialog._feature_buttons[FeatureName.cloud_vision].isEnabled()

    dialog._toggle_feature(FeatureName.cloud_vision)
    assert len(submitted) == 1
    command = submitted[0]
    assert isinstance(command, FeatureSetCommand)
    assert command.feature is FeatureName.cloud_vision
    assert command.enabled is True
    dialog.close()
    qapp.processEvents()


def test_w16_feature_enable_submits_when_pyside_returns_integer_yes(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ManagementViewModel()
    model.apply_event(
        FeatureStatesEvent(states=(FeatureState(name=FeatureName.cloud_vision, enabled=False),))
    )
    submitted: list[ManagementCommand] = []

    def submit(command: ManagementCommand) -> bool:
        submitted.append(command)
        return True

    dialog = SettingsDialog(model, submit)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes)),
    )

    dialog.sync_from_model()
    dialog._toggle_feature(FeatureName.cloud_vision)

    assert len(submitted) == 1
    command = submitted[0]
    assert isinstance(command, FeatureSetCommand)
    assert command.feature is FeatureName.cloud_vision
    assert command.enabled is True
    dialog.close()
    qapp.processEvents()


def test_w16_memory_confirmation_requires_a_second_decision(qapp: QApplication) -> None:
    model = ManagementViewModel()
    model.apply_event(
        MemoryConfirmationsEvent(
            items=(
                MemoryConfirmationSummary(
                    confirmation_id="confirm_w16",
                    content_preview="synthetic proposal",
                    evidence_preview="synthetic evidence",
                    expires_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
                ),
            )
        )
    )
    submitted: list[object] = []

    def submit(command: ManagementCommand) -> bool:
        submitted.append(command)
        return True

    dialog = _ConfirmingSettingsDialog(
        model,
        submit,
        confirmations=[False, True],
    )
    dialog.sync_from_model()
    selected = dialog.confirmation_tree.topLevelItem(0)
    assert selected is not None
    dialog.confirmation_tree.setCurrentItem(selected)

    dialog._confirm_selected(True)
    assert submitted == []

    dialog._confirm_selected(True)
    assert len(submitted) == 1
    command = submitted[0]
    assert isinstance(command, MemoryConfirmCommand)
    assert command.approved is True
    assert command.confirmation_id == "confirm_w16"
    assert selected.data(0, Qt.ItemDataRole.UserRole) == "confirm_w16"
    dialog.close()
    qapp.processEvents()
