"""W17 settings bridge tests with a synthetic MediaWorker device snapshot."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from app.config import Settings
from app.config.settings import LLMConfig, LoggingConfig, StorageConfig
from app.media.types import AudioOutputDevice, OutputDeviceList
from app.paths import AppPaths
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    AudioOutputDevicesCommand,
    AudioOutputDevicesEvent,
    BackendCapabilities,
    DesktopSettingsForm,
    SettingsSaveCommand,
    SettingsSnapshot,
)
from desktop_client.ui.management import (
    DesktopManagementRuntime,
    ManagementViewModel,
    _settings_patch,
)
from desktop_client.ui.settings_dialog import SettingsDialog
from PySide6.QtWidgets import QApplication

_DEVICE_ID = "audio_" + "1" * 32


def _settings(root: Path) -> Settings:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=True, database_path=Path("companion.sqlite3")),
        llm=LLMConfig(provider="mock"),
    )
    settings._paths = AppPaths(root=root)
    return settings


def _form(
    *,
    output_device_id: str = "",
    system_playback_enabled: bool = False,
) -> DesktopSettingsForm:
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
        output_device_id=output_device_id,
        system_playback_enabled=system_playback_enabled,
    )


def test_w17_audio_output_device_command_is_bounded_and_persisted(tmp_path: Path) -> None:
    device = AudioOutputDevice(device_id=_DEVICE_ID, label="Synthetic USB audio", is_default=True)

    async def lister(_settings: Settings) -> OutputDeviceList:
        return OutputDeviceList((device,), False)

    async def scenario() -> AudioOutputDevicesEvent:
        management = DesktopManagementRuntime(
            _settings(tmp_path / "MeguminCompanion"),
            None,
            audio_device_lister=lister,
        )
        bridge = ApplicationBridge()
        command = AudioOutputDevicesCommand()
        await management.dispatch(
            bridge,
            command,
            capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
        )
        events = bridge.drain_events()
        event = next(item for item in events if isinstance(item, AudioOutputDevicesEvent))
        assert event.command_id == command.command_id
        return event

    event = asyncio.run(scenario())
    model = ManagementViewModel()
    assert model.apply_event(event)
    assert model.audio_output_devices == (device,)
    assert model.audio_output_devices_reason is None
    assert _settings_patch(_form(output_device_id=_DEVICE_ID, system_playback_enabled=True))[
        "pipeline"
    ] == {
        "playback_mode": "system",
        "output_device_id": _DEVICE_ID,
    }
    assert _settings_patch(replace(_form(), output_device_id=""))["pipeline"] == {
        "playback_mode": "silent",
        "output_device_id": None,
    }
    with pytest.raises(ValueError, match="output_device_id"):
        _form(output_device_id="not-a-stable-device-id")


def test_w17_audio_device_selector_renders_snapshot_and_submits_id(qapp: QApplication) -> None:
    del qapp
    device = AudioOutputDevice(device_id=_DEVICE_ID, label="Synthetic USB audio", is_default=True)
    model = ManagementViewModel()
    model.settings = SettingsSnapshot(
        form=_form(output_device_id=_DEVICE_ID, system_playback_enabled=True),
        llm_secret_configured=False,
        vts_secret_configured=False,
        settings_schema_upgrade_required=False,
    )
    assert model.apply_event(AudioOutputDevicesEvent(devices=(device,)))
    submitted: list[object] = []

    def submit(command: object) -> bool:
        submitted.append(command)
        return True

    dialog = SettingsDialog(model, submit)

    assert dialog.output_device.currentData() == _DEVICE_ID
    assert "Synthetic USB audio" in dialog.output_device.currentText()
    dialog._refresh_audio_devices()
    assert isinstance(submitted.pop(), AudioOutputDevicesCommand)
    dialog._save_settings()
    save = submitted.pop()
    assert isinstance(save, SettingsSaveCommand)
    assert save.payload.output_device_id == _DEVICE_ID
    assert save.payload.system_playback_enabled
    dialog.clear_sensitive()


def test_w17_audio_device_enumeration_failure_is_visible_to_the_view_model(
    tmp_path: Path,
) -> None:
    async def lister(_settings: Settings) -> OutputDeviceList:
        return OutputDeviceList((), False, "audio_device_enumeration_failed")

    async def scenario() -> AudioOutputDevicesEvent:
        management = DesktopManagementRuntime(
            _settings(tmp_path / "MeguminCompanion"),
            None,
            audio_device_lister=lister,
        )
        bridge = ApplicationBridge()
        await management.dispatch(
            bridge,
            AudioOutputDevicesCommand(),
            capabilities=BackendCapabilities(text_chat=True, turn_cancel=True),
        )
        return next(
            item for item in bridge.drain_events() if isinstance(item, AudioOutputDevicesEvent)
        )

    model = ManagementViewModel()
    assert model.apply_event(asyncio.run(scenario()))
    assert model.audio_output_devices == ()
    assert model.audio_output_devices_reason == "audio_device_enumeration_failed"
