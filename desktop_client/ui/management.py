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
from app.secret_store import SecretStoreError, llm_api_key_file, vts_token_file

from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    MAX_MANAGEMENT_LIST_ITEMS,
    MAX_MANAGEMENT_PREVIEW_CHARS,
    AudioOutputDevicesCommand,
    AudioOutputDevicesEvent,
    BackendCapabilities,
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
)

LOCAL_DESKTOP_USER_ID = "local_user"
_OFFLINE_LLM_PROVIDERS = frozenset({"", "none", "mock"})
_SUPPORTED_TTS_PROVIDERS = frozenset({"mock", "gpt-sovits", "gpt_sovits"})
_DEVICE_INDEX = re.compile(r"^[0-9]+$")


class DesktopSecretStore(Protocol):
    """Write-only DPAPI boundary, with a small fakeable surface for tests."""

    def status(self, settings: Settings) -> tuple[bool, bool]: ...

    def store_llm(self, settings: Settings, value: str) -> None: ...

    async def store_vts(self, settings: Settings, value: str) -> None: ...

    def revoke_llm(self, settings: Settings) -> bool: ...

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
    ) -> None:
        self._settings = settings
        self._memory_runtime = memory_runtime
        self._secrets = secrets or DPAPIDesktopSecretStore()
        self._audio_device_lister = audio_device_lister or _list_audio_output_devices

    @staticmethod
    def handles(command: object) -> TypeGuard[ManagementCommand]:
        return isinstance(
            command,
            (
                ManagementRefreshCommand,
                SettingsSaveCommand,
                AudioOutputDevicesCommand,
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
            if isinstance(command, AudioOutputDevicesCommand):
                await self._publish_audio_output_devices(bridge, command_id=command.command_id)
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
        if provider not in _OFFLINE_LLM_PROVIDERS:
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
            tts_provider != "mock"
            and self._settings.tts.default_preset not in self._settings.tts.presets
        ):
            # W19 owns the real-provider preflight and preset wizard.  W16 may
            # retain an already valid preset configuration, but must not persist
            # a provider choice that makes the next desktop start fail outright.
            self._result(
                bridge,
                operation="settings_save",
                command_id=command.command_id,
                reason_code="tts_preset_required",
            )
            return
        try:
            await asyncio.to_thread(
                patch_user_settings,
                _settings_patch(form),
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
        bridge.publish_event(
            SettingsSnapshotEvent(
                snapshot=SettingsSnapshot(
                    form=_settings_form(self._settings),
                    llm_secret_configured=llm_configured,
                    vts_secret_configured=vts_configured,
                    settings_schema_upgrade_required=self._settings.settings_schema_upgrade_required,
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
                command_id=command_id,
            )
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


async def _list_audio_output_devices(settings: Settings) -> OutputDeviceList:
    """Enumerate only when the user explicitly asks the settings UI to do so."""

    player = MediaWorkerAudioPlayer.for_settings(settings)
    try:
        return await player.list_output_devices()
    finally:
        with suppress(Exception):
            await player.close()


def _settings_form(settings: Settings) -> DesktopSettingsForm:
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
        stt_executable=str(settings.stt.executable),
        stt_model_path=str(settings.stt.model_path),
        stt_device="" if settings.stt.device is None else str(settings.stt.device),
        startup_enabled=settings.desktop.startup_enabled,
        output_device_id=settings.pipeline.output_device_id or "",
        system_playback_enabled=settings.pipeline.playback_mode == "system",
    )


def _settings_patch(form: DesktopSettingsForm) -> dict[str, object]:
    raw_device = form.stt_device.strip()
    device: int | str | None
    if not raw_device:
        device = None
    elif _DEVICE_INDEX.fullmatch(raw_device):
        device = int(raw_device)
    else:
        device = raw_device
    return {
        "desktop": {"startup_enabled": form.startup_enabled},
        "llm": {
            "provider": form.llm_provider.strip(),
            "base_url": form.llm_base_url.strip(),
            "model": form.llm_model.strip(),
        },
        "tts": {
            "provider": form.tts_provider.strip(),
            "base_url": form.tts_base_url.strip(),
        },
        "vts": {
            "enabled": form.vts_enabled,
            "uri": form.vts_uri.strip(),
            "plugin_name": form.vts_plugin_name.strip(),
            "plugin_developer": form.vts_plugin_developer.strip(),
        },
        "stt": {
            "enabled": form.stt_enabled,
            "executable": form.stt_executable.strip(),
            "model_path": form.stt_model_path.strip(),
            "device": device,
        },
        "pipeline": {
            "playback_mode": "system" if form.system_playback_enabled else "silent",
            "output_device_id": form.output_device_id.strip() or None,
        },
    }


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
        self.feature_states.clear()
        self.memory_items = ()
        self.memory_query = ""
        self.memory_list_truncated = False
        self.memory_detail = None
        self.confirmations = ()
        self.confirmations_truncated = False
        self.debug = None
        self.last_result = None
