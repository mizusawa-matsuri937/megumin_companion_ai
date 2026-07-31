"""W16 in-process management commands for settings, features, and memory.

The desktop process never calls the development HTTP API for these operations.
All filesystem, DPAPI, database, and export work remains on the BackendThread;
Qt only submits typed commands and renders bounded response events.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, TypeGuard
from uuid import uuid4

from app import __version__
from app.avatar import AvatarHealthSnapshot, AvatarRuntimeState
from app.clients.vts import DPAPITokenStore, VTSToken
from app.config import ConfigurationError, Settings, load_settings, patch_user_settings
from app.media import MediaWorkerAudioPlayer
from app.media.types import AudioOutputDevice, OutputDeviceList
from app.memory.runtime import MemoryRuntime
from app.memory.service import (
    ConfirmationNotFoundError,
    CredentialRejectedError,
    FeatureDisabledError,
)
from app.schemas import FeatureName, FeatureState
from app.secret_store import (
    SecretStoreError,
    deepseek_api_key_file,
    llm_api_key_file,
    vts_token_file,
)
from app.stt_runtime import (
    ManagedChineseSttRuntime,
    SttRuntimeError,
    SttRuntimeState,
    SttRuntimeStatus,
    managed_stt_settings_patch,
)

from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    MAX_MANAGEMENT_LIST_ITEMS,
    MAX_MANAGEMENT_PREVIEW_CHARS,
    AudioOutputDevicesCommand,
    AudioOutputDevicesEvent,
    AvatarLayerStatus,
    BackendCapabilities,
    DeepSeekFlashConfigureCommand,
    DeepSeekFlashDisableCommand,
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
    ProviderPreflightCheck,
    ProviderPreflightCommand,
    ProviderPreflightEvent,
    ProviderPreflightName,
    SecretRevokeCommand,
    SecretStoreCommand,
    SettingsSaveCommand,
    SettingsSnapshot,
    SettingsSnapshotEvent,
    SttInstallCommand,
)
from desktop_client.ui.provider_preflight import ProviderPreflightRunner

LOCAL_DESKTOP_USER_ID = "local_user"
_OFFLINE_LLM_PROVIDERS = frozenset({"", "none", "mock"})
_DEEPSEEK_FLASH_PROVIDER = "deepseek"
_GATEWAY_TTS_PROVIDERS = frozenset(
    {
        "gpt-sovits-gateway",
        "gpt_sovits_gateway",
        "gateway",
    }
)
_SUPPORTED_TTS_PROVIDERS = frozenset({"mock", "gpt-sovits", "gpt_sovits"}) | (
    _GATEWAY_TTS_PROVIDERS
)
_DEVICE_INDEX = re.compile(r"^[0-9]+$")


def _unavailable_avatar_layers(reason_code: str) -> tuple[AvatarLayerStatus, ...]:
    return (
        AvatarLayerStatus("parameter_control", False, reason_code),
        AvatarLayerStatus("lip_sync", False, reason_code),
        AvatarLayerStatus("body_motion", False, reason_code),
        AvatarLayerStatus("automatic_red_eye", False, reason_code),
    )


class DesktopSecretStore(Protocol):
    """Write-only DPAPI boundary, with a small fakeable surface for tests."""

    def status(self, settings: Settings) -> tuple[bool, bool]: ...

    def store_llm(self, settings: Settings, value: str) -> None: ...

    def deepseek_flash_configured(self, settings: Settings) -> bool: ...

    def store_deepseek_flash(self, settings: Settings, value: str) -> None:
        """Atomically replace the provider-bound key or leave its old slot unchanged."""

    async def store_vts(self, settings: Settings, value: str) -> None: ...

    def revoke_llm(self, settings: Settings) -> bool: ...

    def revoke_deepseek_flash(self, settings: Settings) -> bool: ...

    def revoke_vts(self, settings: Settings) -> bool: ...


class DPAPIDesktopSecretStore:
    """Production write-only secret implementation using current-user DPAPI."""

    def status(self, settings: Settings) -> tuple[bool, bool]:
        return (
            llm_api_key_file(settings.paths).exists,
            vts_token_file(settings.paths, settings.vts_token_path()).exists,
        )

    def store_llm(self, settings: Settings, value: str) -> None:
        llm_api_key_file(settings.paths).write_text(value)

    def deepseek_flash_configured(self, settings: Settings) -> bool:
        return deepseek_api_key_file(settings.paths).exists

    def store_deepseek_flash(self, settings: Settings, value: str) -> None:
        deepseek_api_key_file(settings.paths).write_text(value)

    async def store_vts(self, settings: Settings, value: str) -> None:
        store = DPAPITokenStore(vts_token_file(settings.paths, settings.vts_token_path()))
        await store.save(
            VTSToken(
                plugin_name=settings.vts.plugin_name,
                plugin_developer=settings.vts.plugin_developer,
                authentication_token=value,
            )
        )

    def revoke_llm(self, settings: Settings) -> bool:
        return llm_api_key_file(settings.paths).revoke()

    def revoke_deepseek_flash(self, settings: Settings) -> bool:
        return deepseek_api_key_file(settings.paths).revoke()

    def revoke_vts(self, settings: Settings) -> bool:
        return vts_token_file(settings.paths, settings.vts_token_path()).revoke()


class DesktopManagementRuntime:
    """Serve bounded W16 commands inside the existing application lifespan."""

    def __init__(
        self,
        settings: Settings,
        memory_runtime: MemoryRuntime | None,
        *,
        secrets: DesktopSecretStore | None = None,
        audio_device_lister: Callable[[Settings], Awaitable[OutputDeviceList]] | None = None,
        stt_runtime: ManagedChineseSttRuntime | None = None,
        provider_preflight: ProviderPreflightRunner | None = None,
        avatar_snapshot: Callable[[], object] | None = None,
    ) -> None:
        self._settings = settings
        self._memory_runtime = memory_runtime
        self._secrets = secrets or DPAPIDesktopSecretStore()
        self._audio_device_lister = audio_device_lister or _list_audio_output_devices
        self._uses_default_stt_runtime = stt_runtime is None
        self._stt_runtime = stt_runtime or ManagedChineseSttRuntime(
            settings.paths,
            profile=settings.stt.managed_profile,
        )
        self._provider_preflight = provider_preflight or ProviderPreflightRunner()
        self._avatar_snapshot = avatar_snapshot

    @staticmethod
    def handles(command: object) -> TypeGuard[ManagementCommand]:
        return isinstance(
            command,
            (
                ManagementRefreshCommand,
                SettingsSaveCommand,
                DeepSeekFlashConfigureCommand,
                DeepSeekFlashDisableCommand,
                SttInstallCommand,
                AudioOutputDevicesCommand,
                ProviderPreflightCommand,
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
                ManagementDebugCommand,
            ),
        )

    async def publish_initial(
        self,
        bridge: ApplicationBridge,
        *,
        capabilities: BackendCapabilities,
    ) -> None:
        await self._publish_settings(bridge)
        await self._publish_feature_states(bridge)
        await self._publish_memory_list(bridge, query="")
        await self._publish_confirmations(bridge)
        self._publish_debug(bridge, capabilities=capabilities)

    async def dispatch(
        self,
        bridge: ApplicationBridge,
        command: ManagementCommand,
        *,
        capabilities: BackendCapabilities,
    ) -> None:
        try:
            if isinstance(command, ManagementRefreshCommand):
                await self._publish_settings(bridge, command_id=command.command_id)
                await self._publish_feature_states(bridge, command_id=command.command_id)
                await self._publish_memory_list(bridge, query="", command_id=command.command_id)
                await self._publish_confirmations(bridge, command_id=command.command_id)
                self._publish_debug(
                    bridge,
                    capabilities=capabilities,
                    command_id=command.command_id,
                )
                return
            if isinstance(command, SettingsSaveCommand):
                await self._save_settings(bridge, command)
                return
            if isinstance(command, DeepSeekFlashConfigureCommand):
                await self._configure_deepseek_flash(bridge, command)
                return
            if isinstance(command, DeepSeekFlashDisableCommand):
                await self._disable_deepseek_flash(bridge, command)
                return
            if isinstance(command, SttInstallCommand):
                await self._install_stt_runtime(bridge, command)
                return
            if isinstance(command, AudioOutputDevicesCommand):
                await self._publish_audio_output_devices(bridge, command_id=command.command_id)
                return
            if isinstance(command, ProviderPreflightCommand):
                await self._run_provider_preflight(bridge, command)
                return
            if isinstance(command, SecretStoreCommand):
                await self._store_secret(bridge, command)
                return
            if isinstance(command, SecretRevokeCommand):
                await self._revoke_secret(bridge, command)
                return
            if isinstance(command, FeatureSetCommand):
                await self._set_feature(bridge, command)
                return
            if isinstance(command, MemoryListCommand):
                await self._publish_memory_list(
                    bridge,
                    query=command.query,
                    command_id=command.command_id,
                )
                return
            if isinstance(command, MemoryDetailCommand):
                await self._show_memory_detail(bridge, command)
                return
            if isinstance(command, MemoryUpdateCommand):
                await self._update_memory(bridge, command)
                return
            if isinstance(command, MemoryDeleteCommand):
                await self._delete_memory(bridge, command)
                return
            if isinstance(command, MemoryClearCommand):
                await self._clear_memory(bridge, command)
                return
            if isinstance(command, MemoryConfirmCommand):
                await self._confirm_memory(bridge, command)
                return
            if isinstance(command, MemoryExportCommand):
                await self._export_memories(bridge, command)
                return
            if isinstance(command, ManagementDebugCommand):
                self._publish_debug(
                    bridge,
                    capabilities=capabilities,
                    command_id=command.command_id,
                )
                return
        except Exception as exc:  # pragma: no cover - defensive final boundary
            self._result(
                bridge,
                operation="management_failed",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )

    async def _save_settings(
        self,
        bridge: ApplicationBridge,
        command: SettingsSaveCommand,
    ) -> None:
        form = command.payload
        provider = form.llm_provider.strip().casefold()
        active_deepseek_flash = (
            self._settings.llm.provider.strip().casefold() == _DEEPSEEK_FLASH_PROVIDER
        )
        if provider == _DEEPSEEK_FLASH_PROVIDER:
            if not active_deepseek_flash:
                self._result(
                    bridge,
                    operation="settings_save",
                    command_id=command.command_id,
                    reason_code="deepseek_flash_configure_required",
                )
                return
            try:
                deepseek_flash_configured = self._secrets.deepseek_flash_configured(self._settings)
            except Exception as exc:
                self._result(
                    bridge,
                    operation="settings_save",
                    command_id=command.command_id,
                    reason_code=_management_error_code(exc),
                )
                return
            if not deepseek_flash_configured:
                self._result(
                    bridge,
                    operation="settings_save",
                    command_id=command.command_id,
                    reason_code="deepseek_flash_key_required",
                )
                return
        elif active_deepseek_flash:
            self._result(
                bridge,
                operation="settings_save",
                command_id=command.command_id,
                reason_code="deepseek_flash_disable_required",
            )
            return
        elif provider not in _OFFLINE_LLM_PROVIDERS:
            if not form.llm_model.strip():
                self._result(
                    bridge,
                    operation="settings_save",
                    command_id=command.command_id,
                    reason_code="llm_model_required",
                )
                return
            try:
                llm_configured, _ = self._secrets.status(self._settings)
            except Exception as exc:
                self._result(
                    bridge,
                    operation="settings_save",
                    command_id=command.command_id,
                    reason_code=_management_error_code(exc),
                )
                return
            if not llm_configured:
                self._result(
                    bridge,
                    operation="settings_save",
                    command_id=command.command_id,
                    reason_code="secret_required",
                )
                return
        tts_provider = form.tts_provider.strip().casefold()
        if tts_provider not in _SUPPORTED_TTS_PROVIDERS:
            self._result(
                bridge,
                operation="settings_save",
                command_id=command.command_id,
                reason_code="tts_provider_unsupported",
            )
            return
        if (
            tts_provider not in {"mock"} | _GATEWAY_TTS_PROVIDERS
            and not form.tts_ref_audio_path.strip()
        ):
            self._result(
                bridge,
                operation="settings_save",
                command_id=command.command_id,
                reason_code="tts_reference_required",
            )
            return
        try:
            await asyncio.to_thread(
                patch_user_settings,
                _settings_patch(form, include_llm=not active_deepseek_flash),
                app_paths=self._settings.paths,
            )
            self._settings = await asyncio.to_thread(
                load_settings,
                app_paths=self._settings.paths,
                environ={},
            )
            self._refresh_default_stt_runtime()
        except Exception as exc:
            self._result(
                bridge,
                operation="settings_save",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        await self._publish_settings(bridge, command_id=command.command_id)
        self._result(
            bridge,
            operation="settings_saved",
            command_id=command.command_id,
            restart_required=True,
        )

    async def _configure_deepseek_flash(
        self,
        bridge: ApplicationBridge,
        command: DeepSeekFlashConfigureCommand,
    ) -> None:
        """Activate only the fixed Flash profile and its isolated DPAPI key.

        The runtime consumes persisted settings only after a restart.  Patching
        the provider before storing the key therefore cannot issue an outbound
        request.  Reload settings before the DPAPI write, so every failure that
        can occur after patching happens before a credential is changed.  The
        production secret store restores its encrypted pre-write envelope if
        post-replace verification fails; this orchestration adds a revoke
        fallback for a previously absent slot.
        """

        if self._settings.memory.candidate_analysis_enabled:
            self._result(
                bridge,
                operation="deepseek_flash_configure",
                command_id=command.command_id,
                reason_code="deepseek_memory_pro_required",
            )
            return

        previous_provider = self._settings.llm.provider
        provider_patched = False
        key_write_started = False
        had_existing_key = False
        try:
            await asyncio.to_thread(
                patch_user_settings,
                {"llm": {"provider": _DEEPSEEK_FLASH_PROVIDER}},
                app_paths=self._settings.paths,
            )
            provider_patched = True
            updated_settings = await asyncio.to_thread(
                load_settings,
                app_paths=self._settings.paths,
                environ={},
            )
            had_existing_key = await asyncio.to_thread(
                self._secrets.deepseek_flash_configured,
                updated_settings,
            )
            key_write_started = True
            await asyncio.to_thread(
                self._secrets.store_deepseek_flash,
                updated_settings,
                command.value,
            )
        except Exception as exc:
            provider_rollback_failed = False
            if provider_patched:
                try:
                    await asyncio.to_thread(
                        patch_user_settings,
                        {"llm": {"provider": previous_provider}},
                        app_paths=self._settings.paths,
                    )
                    self._settings = await asyncio.to_thread(
                        load_settings,
                        app_paths=self._settings.paths,
                        environ={},
                    )
                except Exception:
                    provider_rollback_failed = True
            key_rollback_failed = False
            if key_write_started and not had_existing_key:
                try:
                    await asyncio.to_thread(
                        self._secrets.revoke_deepseek_flash,
                        updated_settings,
                    )
                except Exception:
                    key_rollback_failed = True
            if provider_rollback_failed or key_rollback_failed:
                self._result(
                    bridge,
                    operation="deepseek_flash_configure",
                    command_id=command.command_id,
                    reason_code="deepseek_flash_rollback_failed",
                )
                return
            self._result(
                bridge,
                operation="deepseek_flash_configure",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return

        self._settings = updated_settings
        await self._publish_settings(bridge, command_id=command.command_id)
        self._result(
            bridge,
            operation="deepseek_flash_configured",
            command_id=command.command_id,
            restart_required=True,
        )

    async def _disable_deepseek_flash(
        self,
        bridge: ApplicationBridge,
        command: DeepSeekFlashDisableCommand,
    ) -> None:
        """Disable remote use before deleting an isolated DeepSeek credential."""

        active_deepseek_flash = (
            self._settings.llm.provider.strip().casefold() == _DEEPSEEK_FLASH_PROVIDER
        )
        if active_deepseek_flash:
            try:
                await asyncio.to_thread(
                    patch_user_settings,
                    {"llm": {"provider": "none"}},
                    app_paths=self._settings.paths,
                )
                self._settings = await asyncio.to_thread(
                    load_settings,
                    app_paths=self._settings.paths,
                    environ={},
                )
            except Exception as exc:
                self._result(
                    bridge,
                    operation="deepseek_flash_disable",
                    command_id=command.command_id,
                    reason_code=_management_error_code(exc),
                )
                return

        try:
            await asyncio.to_thread(self._secrets.revoke_deepseek_flash, self._settings)
        except Exception:
            # The provider has already been switched to the offline safe value;
            # publish that fact even if the stale encrypted file cannot be removed.
            await self._publish_settings(bridge, command_id=command.command_id)
            self._result(
                bridge,
                operation="deepseek_flash_disabled",
                command_id=command.command_id,
                reason_code="deepseek_flash_key_revoke_failed",
                restart_required=active_deepseek_flash,
            )
            return

        await self._publish_settings(bridge, command_id=command.command_id)
        self._result(
            bridge,
            operation="deepseek_flash_disabled",
            command_id=command.command_id,
            restart_required=active_deepseek_flash,
        )

    async def _install_stt_runtime(
        self,
        bridge: ApplicationBridge,
        command: SttInstallCommand,
    ) -> None:
        """Run the explicit managed installer entirely on BackendThread."""

        install_task = asyncio.create_task(
            self._stt_runtime.install(),
            name="managed-chinese-stt-install",
        )
        # Let the installer enter its in-flight state before publishing a
        # snapshot so Qt can disable duplicate activation immediately.
        await asyncio.sleep(0)
        await self._publish_settings(bridge, command_id=command.command_id)
        try:
            await install_task
            await asyncio.to_thread(
                patch_user_settings,
                managed_stt_settings_patch(self._settings.stt.managed_profile),
                app_paths=self._settings.paths,
            )
            self._settings = await asyncio.to_thread(
                load_settings,
                app_paths=self._settings.paths,
                environ={},
            )
            self._refresh_default_stt_runtime()
        except Exception as exc:
            self._result(
                bridge,
                operation="stt_runtime_install",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        await self._publish_settings(bridge, command_id=command.command_id)
        self._result(
            bridge,
            operation="stt_runtime_installed",
            command_id=command.command_id,
            restart_required=True,
        )

    async def _run_provider_preflight(
        self,
        bridge: ApplicationBridge,
        command: ProviderPreflightCommand,
    ) -> None:
        """Probe persisted provider settings without exposing response bodies."""

        def publish(checks: tuple[ProviderPreflightCheck, ...]) -> None:
            bridge.publish_event(
                ProviderPreflightEvent(
                    checks=checks,
                    command_id=command.command_id,
                )
            )

        try:
            await self._provider_preflight.run(self._settings, publish=publish)
        except Exception as exc:
            self._result(
                bridge,
                operation="provider_preflight",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        self._result(
            bridge,
            operation="provider_preflight_completed",
            command_id=command.command_id,
        )

    async def _store_secret(
        self,
        bridge: ApplicationBridge,
        command: SecretStoreCommand,
    ) -> None:
        try:
            if command.secret_id == "llm":
                await asyncio.to_thread(self._secrets.store_llm, self._settings, command.value)
            else:
                await self._secrets.store_vts(self._settings, command.value)
        except Exception as exc:
            self._result(
                bridge,
                operation="secret_store",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        await self._publish_settings(bridge, command_id=command.command_id)
        self._result(bridge, operation="secret_stored", command_id=command.command_id)

    async def _revoke_secret(
        self,
        bridge: ApplicationBridge,
        command: SecretRevokeCommand,
    ) -> None:
        try:
            if command.secret_id == "llm":
                await asyncio.to_thread(self._secrets.revoke_llm, self._settings)
            else:
                await asyncio.to_thread(self._secrets.revoke_vts, self._settings)
        except Exception as exc:
            self._result(
                bridge,
                operation="secret_revoke",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        await self._publish_settings(bridge, command_id=command.command_id)
        self._result(bridge, operation="secret_revoked", command_id=command.command_id)

    async def _set_feature(
        self,
        bridge: ApplicationBridge,
        command: FeatureSetCommand,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            self._unavailable(bridge, command.command_id, "feature_set")
            return

        async def publish_transition(_state: FeatureState) -> None:
            # Request-transition persistence happens before this callback.  A
            # complete snapshot lets the UI show desired/actual transition
            # state without replacing its other feature rows with a partial
            # update.
            try:
                states = tuple(await runtime.list_features())
                bridge.publish_event(
                    FeatureStatesEvent(states=states, command_id=command.command_id)
                )
            except Exception:
                # Management observation must not change the real feature
                # transition outcome. The final state/result path still
                # reports a stable failure if the state machine itself fails.
                return

        try:
            state = await runtime.set_feature(
                command.feature,
                command.enabled,
                on_transition=publish_transition,
            )
        except Exception as exc:
            self._result(
                bridge,
                operation="feature_set",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        await self._publish_feature_states(bridge, command_id=command.command_id)
        self._result(
            bridge,
            operation="feature_set",
            command_id=command.command_id,
            reason_code=state.reason_code,
        )

    async def _publish_feature_states(
        self,
        bridge: ApplicationBridge,
        *,
        command_id: str | None = None,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            if command_id is not None:
                self._unavailable(bridge, command_id, "feature_list")
            return
        try:
            states = tuple(await runtime.list_features())
        except Exception as exc:
            if command_id is not None:
                self._result(
                    bridge,
                    operation="feature_list",
                    command_id=command_id,
                    reason_code=_management_error_code(exc),
                )
            return
        if states:
            bridge.publish_event(FeatureStatesEvent(states=states, command_id=command_id))

    async def _publish_memory_list(
        self,
        bridge: ApplicationBridge,
        *,
        query: str,
        command_id: str | None = None,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            if command_id is not None:
                self._unavailable(bridge, command_id, "memory_list")
            return
        try:
            if query.strip():
                items = await runtime.search_memories(
                    user_id=LOCAL_DESKTOP_USER_ID,
                    query=query,
                    limit=MAX_MANAGEMENT_LIST_ITEMS + 1,
                )
            else:
                items = await runtime.list_memories(
                    user_id=LOCAL_DESKTOP_USER_ID,
                    limit=MAX_MANAGEMENT_LIST_ITEMS + 1,
                )
        except Exception as exc:
            if command_id is not None:
                self._result(
                    bridge,
                    operation="memory_list",
                    command_id=command_id,
                    reason_code=_management_error_code(exc),
                )
            return
        bridge.publish_event(
            MemoryListEvent(
                items=tuple(_memory_summary(item) for item in items[:MAX_MANAGEMENT_LIST_ITEMS]),
                query=query,
                truncated=len(items) > MAX_MANAGEMENT_LIST_ITEMS,
                command_id=command_id,
            )
        )

    async def _show_memory_detail(
        self,
        bridge: ApplicationBridge,
        command: MemoryDetailCommand,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            self._unavailable(bridge, command.command_id, "memory_detail")
            return
        try:
            item = await runtime.get_memory(command.memory_id, user_id=LOCAL_DESKTOP_USER_ID)
        except Exception as exc:
            bridge.publish_event(
                MemoryDetailEvent(
                    memory_id=command.memory_id,
                    item=None,
                    reason_code=_management_error_code(exc),
                    command_id=command.command_id,
                )
            )
            return
        bridge.publish_event(
            MemoryDetailEvent(
                memory_id=command.memory_id,
                item=item,
                reason_code=None if item is not None else "memory_not_found",
                command_id=command.command_id,
            )
        )

    async def _update_memory(
        self,
        bridge: ApplicationBridge,
        command: MemoryUpdateCommand,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            self._unavailable(bridge, command.command_id, "memory_update")
            return
        try:
            item = await runtime.update_memory(
                command.memory_id,
                user_id=LOCAL_DESKTOP_USER_ID,
                content=command.content,
            )
        except Exception as exc:
            self._result(
                bridge,
                operation="memory_update",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        if item is None:
            self._result(
                bridge,
                operation="memory_update",
                command_id=command.command_id,
                reason_code="memory_not_found",
            )
            return
        bridge.publish_event(
            MemoryDetailEvent(
                memory_id=item.memory_id,
                item=item,
                command_id=command.command_id,
            )
        )
        await self._publish_memory_list(bridge, query="")
        self._result(bridge, operation="memory_updated", command_id=command.command_id)

    async def _delete_memory(
        self,
        bridge: ApplicationBridge,
        command: MemoryDeleteCommand,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            self._unavailable(bridge, command.command_id, "memory_delete")
            return
        try:
            result = await runtime.delete_memory(command.memory_id, user_id=LOCAL_DESKTOP_USER_ID)
        except Exception as exc:
            self._result(
                bridge,
                operation="memory_delete",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        if not result.logical_deleted:
            self._result(
                bridge,
                operation="memory_delete",
                command_id=command.command_id,
                reason_code="memory_not_found",
            )
            return
        bridge.publish_event(
            MemoryDetailEvent(
                memory_id=command.memory_id,
                item=None,
                reason_code="memory_not_found",
                command_id=command.command_id,
            )
        )
        await self._publish_memory_list(bridge, query="")
        self._result(
            bridge,
            operation="memory_deleted",
            command_id=command.command_id,
            deleted_count=result.deleted_count,
            cleanup_pending=result.cleanup_pending,
        )

    async def _clear_memory(
        self,
        bridge: ApplicationBridge,
        command: MemoryClearCommand,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            self._unavailable(bridge, command.command_id, "memory_clear")
            return
        try:
            result = (
                await runtime.clear_history(user_id=LOCAL_DESKTOP_USER_ID)
                if command.target == "history"
                else await runtime.clear_memories(user_id=LOCAL_DESKTOP_USER_ID)
            )
        except Exception as exc:
            self._result(
                bridge,
                operation="memory_clear",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        if command.target == "memories":
            await self._publish_memory_list(bridge, query="")
            await self._publish_confirmations(bridge)
        self._result(
            bridge,
            operation=("history_cleared" if command.target == "history" else "memories_cleared"),
            command_id=command.command_id,
            deleted_count=result.deleted_count,
            cleanup_pending=result.cleanup_pending,
        )

    async def _publish_confirmations(
        self,
        bridge: ApplicationBridge,
        *,
        command_id: str | None = None,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            if command_id is not None:
                self._unavailable(bridge, command_id, "memory_confirmations")
            return
        try:
            pending = await runtime.pending_confirmations()
        except Exception as exc:
            if command_id is not None:
                self._result(
                    bridge,
                    operation="memory_confirmations",
                    command_id=command_id,
                    reason_code=_management_error_code(exc),
                )
            return
        bridge.publish_event(
            MemoryConfirmationsEvent(
                items=tuple(
                    _confirmation_summary(item) for item in pending[:MAX_MANAGEMENT_LIST_ITEMS]
                ),
                truncated=len(pending) > MAX_MANAGEMENT_LIST_ITEMS,
                command_id=command_id,
            )
        )

    async def _confirm_memory(
        self,
        bridge: ApplicationBridge,
        command: MemoryConfirmCommand,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            self._unavailable(bridge, command.command_id, "memory_confirm")
            return
        try:
            await runtime.confirm_memory(command.confirmation_id, approved=command.approved)
        except Exception as exc:
            self._result(
                bridge,
                operation="memory_confirm",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        await self._publish_confirmations(bridge, command_id=command.command_id)
        await self._publish_memory_list(bridge, query="")
        self._result(bridge, operation="memory_confirmed", command_id=command.command_id)

    async def _export_memories(
        self,
        bridge: ApplicationBridge,
        command: MemoryExportCommand,
    ) -> None:
        runtime = self._memory_runtime
        if runtime is None:
            self._unavailable(bridge, command.command_id, "memory_export")
            return
        try:
            payload = await runtime.export(user_id=LOCAL_DESKTOP_USER_ID)
            await asyncio.to_thread(
                _write_memory_export,
                command.destination,
                payload,
                overwrite=command.overwrite,
            )
        except Exception as exc:
            self._result(
                bridge,
                operation="memory_export",
                command_id=command.command_id,
                reason_code=_management_error_code(exc),
            )
            return
        self._result(bridge, operation="memory_exported", command_id=command.command_id)

    async def _publish_settings(
        self,
        bridge: ApplicationBridge,
        *,
        command_id: str | None = None,
    ) -> None:
        try:
            llm_configured, vts_configured = await asyncio.to_thread(
                self._secrets.status,
                self._settings,
            )
        except Exception as exc:
            llm_configured = False
            vts_configured = False
            if command_id is not None:
                self._result(
                    bridge,
                    operation="settings_status",
                    command_id=command_id,
                    reason_code=_management_error_code(exc),
                )
        try:
            deepseek_flash_configured = await asyncio.to_thread(
                self._secrets.deepseek_flash_configured,
                self._settings,
            )
        except Exception as exc:
            deepseek_flash_configured = False
            if command_id is not None:
                self._result(
                    bridge,
                    operation="deepseek_flash_status",
                    command_id=command_id,
                    reason_code=_management_error_code(exc),
                )
        try:
            stt_runtime = await asyncio.to_thread(
                self._stt_runtime.status_for_settings,
                self._settings,
            )
        except Exception:
            stt_runtime = SttRuntimeStatus(SttRuntimeState.integrity_failed)
        bridge.publish_event(
            SettingsSnapshotEvent(
                snapshot=SettingsSnapshot(
                    form=_settings_form(self._settings),
                    llm_secret_configured=llm_configured,
                    vts_secret_configured=vts_configured,
                    settings_schema_upgrade_required=self._settings.settings_schema_upgrade_required,
                    deepseek_flash_configured=deepseek_flash_configured,
                    stt_runtime=stt_runtime,
                ),
                command_id=command_id,
            )
        )

    async def _publish_audio_output_devices(
        self,
        bridge: ApplicationBridge,
        *,
        command_id: str,
    ) -> None:
        try:
            listing = await self._audio_device_lister(self._settings)
        except Exception:
            bridge.publish_event(
                AudioOutputDevicesEvent(
                    devices=(),
                    reason_code="audio_device_enumeration_failed",
                    command_id=command_id,
                )
            )
            return
        bridge.publish_event(
            AudioOutputDevicesEvent(
                devices=listing.devices,
                truncated=listing.truncated,
                reason_code=listing.reason_code,
                command_id=command_id,
            )
        )

    def _publish_debug(
        self,
        bridge: ApplicationBridge,
        *,
        capabilities: BackendCapabilities,
        command_id: str | None = None,
    ) -> None:
        bridge.publish_event(
            ManagementDebugEvent(
                version=__version__,
                capabilities=capabilities,
                command_queue_count=bridge.command_count,
                command_queue_capacity=bridge.command_capacity,
                event_queue_count=bridge.event_count,
                event_queue_capacity=bridge.event_capacity,
                avatar_layers=self._avatar_layers(),
                command_id=command_id,
            )
        )

    def _avatar_layers(self) -> tuple[AvatarLayerStatus, ...]:
        source = self._avatar_snapshot
        if source is None:
            return _unavailable_avatar_layers("avatar_disabled")
        try:
            snapshot = source()
        except Exception:
            return _unavailable_avatar_layers("avatar_status_unavailable")
        if not isinstance(snapshot, AvatarHealthSnapshot):
            return _unavailable_avatar_layers("avatar_status_unavailable")

        fallback = snapshot.error_code
        if fallback is None and snapshot.state is not AvatarRuntimeState.ready:
            fallback = {
                AvatarRuntimeState.disabled: "avatar_disabled",
                AvatarRuntimeState.stopped: "avatar_stopped",
            }.get(snapshot.state, "avatar_not_ready")
        return (
            AvatarLayerStatus(
                "parameter_control",
                snapshot.parameter_control_available,
                snapshot.parameter_error_code
                or (None if snapshot.parameter_control_available else fallback),
            ),
            AvatarLayerStatus(
                "lip_sync",
                snapshot.lip_sync_available,
                snapshot.lip_sync_error_code or (None if snapshot.lip_sync_available else fallback),
            ),
            AvatarLayerStatus(
                "body_motion",
                snapshot.body_motion_available,
                snapshot.body_motion_error_code
                or (None if snapshot.body_motion_available else fallback),
            ),
            AvatarLayerStatus(
                "automatic_red_eye",
                snapshot.automatic_red_eye_available,
                snapshot.red_eye_error_code
                or (None if snapshot.automatic_red_eye_available else fallback),
            ),
        )

    @staticmethod
    def _result(
        bridge: ApplicationBridge,
        *,
        operation: str,
        command_id: str,
        reason_code: str | None = None,
        deleted_count: int = 0,
        cleanup_pending: bool = False,
        restart_required: bool = False,
    ) -> None:
        bridge.publish_event(
            ManagementResultEvent(
                operation=operation,
                command_id=command_id,
                reason_code=reason_code,
                deleted_count=deleted_count,
                cleanup_pending=cleanup_pending,
                restart_required=restart_required,
            )
        )

    def _unavailable(self, bridge: ApplicationBridge, command_id: str, operation: str) -> None:
        self._result(
            bridge,
            operation=operation,
            command_id=command_id,
            reason_code="private_state_runtime_disabled",
        )

    def _refresh_default_stt_runtime(self) -> None:
        """Bind the next explicit install to the newly saved profile selection.

        Test doubles remain intact; production only replaces its small
        management service after the persisted, validated settings reload.
        """

        if self._uses_default_stt_runtime:
            self._stt_runtime = ManagedChineseSttRuntime(
                self._settings.paths,
                profile=self._settings.stt.managed_profile,
            )


async def _list_audio_output_devices(settings: Settings) -> OutputDeviceList:
    """Enumerate only when the user explicitly asks the settings UI to do so."""

    player = MediaWorkerAudioPlayer.for_settings(settings)
    try:
        return await player.list_output_devices()
    finally:
        with suppress(Exception):
            await player.close()


def _settings_form(settings: Settings) -> DesktopSettingsForm:
    preset = settings.tts.presets.get(settings.tts.default_preset)
    return DesktopSettingsForm(
        llm_provider=settings.llm.provider,
        llm_base_url=settings.llm.base_url,
        llm_model=settings.llm.model,
        tts_provider=settings.tts.provider,
        tts_base_url=settings.tts.base_url,
        vts_enabled=settings.vts.enabled,
        vts_uri=settings.vts.uri,
        vts_plugin_name=settings.vts.plugin_name,
        vts_plugin_developer=settings.vts.plugin_developer,
        stt_enabled=settings.stt.enabled,
        stt_profile=settings.stt.managed_profile,
        stt_executable=str(settings.stt.executable),
        stt_model_path=str(settings.stt.model_path),
        stt_device="" if settings.stt.device is None else str(settings.stt.device),
        startup_enabled=settings.desktop.startup_enabled,
        output_device_id=settings.pipeline.output_device_id or "",
        system_playback_enabled=settings.pipeline.playback_mode == "system",
        tts_preset_name=settings.tts.default_preset,
        tts_ref_audio_path="" if preset is None else preset.ref_audio_path,
        tts_ref_audio_scope=("service_resource" if preset is None else preset.ref_audio_scope),
        tts_prompt_text="" if preset is None else preset.prompt_text,
        tts_prompt_lang="zh" if preset is None else preset.prompt_lang,
        avatar_enabled=settings.avatar.enabled,
        avatar_parameter_control_enabled=settings.avatar.parameter_control_enabled,
        avatar_micro_motion_enabled=settings.avatar.micro_motion_enabled,
        avatar_lip_sync_enabled=settings.avatar.lip_sync_enabled,
        avatar_body_motion_enabled=settings.avatar.body_motion_enabled,
        avatar_auto_red_eye_enabled=settings.avatar.auto_red_eye_enabled,
        avatar_mouth_noise_floor=settings.avatar.mouth_noise_floor,
        avatar_mouth_gain=settings.avatar.mouth_gain,
        avatar_mouth_attack_seconds=settings.avatar.mouth_attack_seconds,
        avatar_mouth_release_seconds=settings.avatar.mouth_release_seconds,
    )


def _settings_patch(
    form: DesktopSettingsForm,
    *,
    include_llm: bool = True,
) -> dict[str, object]:
    raw_device = form.stt_device.strip()
    device: int | str | None
    if not raw_device:
        device = None
    elif _DEVICE_INDEX.fullmatch(raw_device):
        device = int(raw_device)
    else:
        device = raw_device
    patch: dict[str, object] = {
        "desktop": {"startup_enabled": form.startup_enabled},
        "tts": {
            "provider": form.tts_provider.strip(),
            "base_url": form.tts_base_url.strip(),
            "default_preset": form.tts_preset_name.strip(),
            "presets": (
                {
                    form.tts_preset_name.strip(): {
                        "ref_audio_path": form.tts_ref_audio_path.strip(),
                        "ref_audio_scope": form.tts_ref_audio_scope,
                        "prompt_text": form.tts_prompt_text,
                        "prompt_lang": form.tts_prompt_lang.strip(),
                        "text_lang": "zh",
                    }
                }
                if form.tts_ref_audio_path.strip()
                else {}
            ),
        },
        "vts": {
            "enabled": form.vts_enabled,
            "uri": form.vts_uri.strip(),
            "plugin_name": form.vts_plugin_name.strip(),
            "plugin_developer": form.vts_plugin_developer.strip(),
        },
        "stt": {
            "enabled": form.stt_enabled,
            "managed_profile": form.stt_profile,
            "executable": form.stt_executable.strip(),
            "model_path": form.stt_model_path.strip(),
            "device": device,
        },
        "pipeline": {
            "playback_mode": "system" if form.system_playback_enabled else "silent",
            "output_device_id": form.output_device_id.strip() or None,
        },
        "avatar": {
            "enabled": form.avatar_enabled,
            "parameter_control_enabled": form.avatar_parameter_control_enabled,
            "micro_motion_enabled": form.avatar_micro_motion_enabled,
            "lip_sync_enabled": form.avatar_lip_sync_enabled,
            "body_motion_enabled": form.avatar_body_motion_enabled,
            "auto_red_eye_enabled": form.avatar_auto_red_eye_enabled,
            "mouth_noise_floor": form.avatar_mouth_noise_floor,
            "mouth_gain": form.avatar_mouth_gain,
            "mouth_attack_seconds": form.avatar_mouth_attack_seconds,
            "mouth_release_seconds": form.avatar_mouth_release_seconds,
        },
    }
    if include_llm:
        patch["llm"] = {
            "provider": form.llm_provider.strip(),
            "base_url": form.llm_base_url.strip(),
            "model": form.llm_model.strip(),
        }
    return patch


def _memory_summary(item: Any) -> MemorySummary:
    return MemorySummary(
        memory_id=item.memory_id,
        memory_type=item.memory_type,
        content_preview=_preview(item.content),
        sensitivity=item.sensitivity,
        status=item.status,
        updated_at=item.updated_at,
    )


def _confirmation_summary(item: Any) -> MemoryConfirmationSummary:
    proposal = item.evaluation.proposal
    return MemoryConfirmationSummary(
        confirmation_id=item.confirmation_id,
        content_preview=_preview(proposal.claim.content),
        evidence_preview=_preview(proposal.claim.evidence_quote),
        expires_at=item.expires_at,
    )


def _preview(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= MAX_MANAGEMENT_PREVIEW_CHARS:
        return normalized
    return normalized[: MAX_MANAGEMENT_PREVIEW_CHARS - 1] + "…"


def _write_memory_export(destination: str, payload: dict[str, Any], *, overwrite: bool) -> None:
    target = Path(destination).expanduser()
    if not target.is_absolute() or not target.name or target.name in {".", ".."}:
        raise _ManagementFailure("export_path_invalid")
    parent = target.parent
    if not parent.is_dir():
        raise _ManagementFailure("export_directory_unavailable")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise _ManagementFailure("export_destination_invalid")
    if target.exists() and not overwrite:
        raise _ManagementFailure("export_destination_exists")
    temporary = parent / f".{target.name}.{uuid4().hex}.part"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except _ManagementFailure:
        raise
    except OSError as exc:
        raise _ManagementFailure("export_write_failed") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


class _ManagementFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _management_error_code(exc: Exception) -> str:
    if isinstance(exc, SttRuntimeError):
        return exc.code
    if isinstance(exc, _ManagementFailure):
        return exc.code
    if isinstance(exc, SecretStoreError):
        return exc.code.value
    if isinstance(exc, CredentialRejectedError):
        return "credential_content_forbidden"
    if isinstance(exc, ConfirmationNotFoundError):
        return "confirmation_not_found"
    if isinstance(exc, FeatureDisabledError):
        return "long_term_memory_disabled"
    if isinstance(exc, ConfigurationError):
        return "settings_invalid"
    if isinstance(exc, ValueError):
        return "invalid_management_input"
    return "management_failed"


class ManagementViewModel:
    """Qt-free, in-memory-only presentation state for W16 management surfaces."""

    def __init__(self) -> None:
        self.settings: SettingsSnapshot | None = None
        self.audio_output_devices: tuple[AudioOutputDevice, ...] = ()
        self.audio_output_devices_truncated = False
        self.audio_output_devices_reason: str | None = None
        self.provider_preflight_checks: dict[ProviderPreflightName, ProviderPreflightCheck] = {}
        self.feature_states: dict[FeatureName, FeatureState] = {}
        self.memory_items: tuple[MemorySummary, ...] = ()
        self.memory_query = ""
        self.memory_list_truncated = False
        self.memory_detail: MemoryDetailEvent | None = None
        self.confirmations: tuple[MemoryConfirmationSummary, ...] = ()
        self.confirmations_truncated = False
        self.debug: ManagementDebugEvent | None = None
        self.last_result: ManagementResultEvent | None = None

    def apply_event(self, event: object) -> bool:
        if isinstance(event, SettingsSnapshotEvent):
            self.settings = event.snapshot
            return True
        if isinstance(event, AudioOutputDevicesEvent):
            self.audio_output_devices = event.devices
            self.audio_output_devices_truncated = event.truncated
            self.audio_output_devices_reason = event.reason_code
            return True
        if isinstance(event, ProviderPreflightEvent):
            self.provider_preflight_checks = {check.name: check for check in event.checks}
            return True
        if isinstance(event, FeatureStatesEvent):
            self.feature_states = {state.name: state for state in event.states}
            return True
        if isinstance(event, MemoryListEvent):
            self.memory_items = event.items
            self.memory_query = event.query
            self.memory_list_truncated = event.truncated
            return True
        if isinstance(event, MemoryDetailEvent):
            self.memory_detail = event
            return True
        if isinstance(event, MemoryConfirmationsEvent):
            self.confirmations = event.items
            self.confirmations_truncated = event.truncated
            return True
        if isinstance(event, ManagementDebugEvent):
            self.debug = event
            return True
        if isinstance(event, ManagementResultEvent):
            self.last_result = event
            return True
        return False

    def clear_sensitive(self) -> None:
        """Drop UI-held paths, memory bodies, summaries, and status after close."""

        self.settings = None
        self.audio_output_devices = ()
        self.audio_output_devices_truncated = False
        self.audio_output_devices_reason = None
        self.provider_preflight_checks.clear()
        self.feature_states.clear()
        self.memory_items = ()
        self.memory_query = ""
        self.memory_list_truncated = False
        self.memory_detail = None
        self.confirmations = ()
        self.confirmations_truncated = False
        self.debug = None
        self.last_result = None
