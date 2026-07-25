from __future__ import annotations

import asyncio
import io
import json
import wave
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from app.clients.tts import GPTSoVITSPreset, GPTSoVITSProbe, GPTSoVITSProvider
from app.clients.vts import VTSBridgeSnapshot, VTSBridgeState
from app.config import Settings, read_user_settings
from app.config.settings import GPTSoVITSPresetConfig, TTSConfig, VTSConfig
from app.core import CancellationToken
from app.paths import AppPaths
from app.schemas import AudioResult, TTSJob
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    DesktopSettingsForm,
    ManagementCommand,
    ManagementResultEvent,
    ProviderPreflightCheck,
    ProviderPreflightCommand,
    ProviderPreflightEvent,
    ProviderPreflightState,
    SettingsSaveCommand,
    SettingsSnapshot,
)
from desktop_client.ui.management import DesktopManagementRuntime, ManagementViewModel
from desktop_client.ui.provider_preflight import (
    ProviderPreflightRunner,
    VTSPreflightBridge,
    _vts_checks,
)
from desktop_client.ui.settings_dialog import SettingsDialog
from PySide6.QtWidgets import QApplication

_REMOTE_REFERENCE = "/srv/private/voice/reference-w19.wav"
_PRIVATE_PROMPT = "private prompt must stay outside preflight events"


def _wave_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * 160)
    return output.getvalue()


def _settings(
    root: Path,
    *,
    tts_provider: str = "mock",
    presets: dict[str, GPTSoVITSPresetConfig] | None = None,
    vts_enabled: bool = False,
) -> Settings:
    settings = Settings(
        tts=TTSConfig(
            provider=tts_provider,
            presets={} if presets is None else presets,
            connect_timeout_seconds=1.0,
            first_byte_timeout_seconds=1.0,
            timeout_seconds=2.0,
        ),
        vts=VTSConfig(enabled=vts_enabled, request_timeout_seconds=1.0),
    )
    settings._paths = AppPaths(root=root)
    return settings


def _snapshot(
    state: VTSBridgeState,
    *,
    error_code: str | None = None,
    api_available: bool = False,
    authenticated: bool = False,
    model_loaded: bool = False,
    missing_hotkey_count: int = 0,
) -> VTSBridgeSnapshot:
    return VTSBridgeSnapshot(
        state=state,
        queue_size=0,
        dropped_actions=0,
        processed_actions=0,
        reconnect_count=0,
        missing_expression_count=0,
        error_code=error_code,
        api_available=api_available,
        authenticated=authenticated,
        model_loaded=model_loaded,
        missing_hotkey_count=missing_hotkey_count,
    )


def _final(
    updates: list[tuple[ProviderPreflightCheck, ...]],
) -> dict[str, ProviderPreflightCheck]:
    return {check.name: check for check in updates[-1]}


def test_gpt_sovits_preflight_uses_fixed_text_discards_wav_and_hides_provider_data(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        requests: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert isinstance(payload, dict)
            requests.append(payload)
            if not payload:
                return httpx.Response(422, request=request)
            return httpx.Response(
                200,
                request=request,
                content=_wave_bytes(),
                headers={"content-type": "audio/wav"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        output = tmp_path / "preflight-output"

        def tts_factory(_settings: Settings) -> GPTSoVITSProvider:
            return GPTSoVITSProvider(
                "http://127.0.0.1:9880",
                output,
                {
                    "default": GPTSoVITSPreset(
                        ref_audio_path=_REMOTE_REFERENCE,
                        prompt_text=_PRIVATE_PROMPT,
                    )
                },
                client=client,
                max_owned_synthesis_tasks=1,
            )

        updates: list[tuple[ProviderPreflightCheck, ...]] = []
        runner = ProviderPreflightRunner(tts_factory=tts_factory)
        settings = _settings(
            tmp_path / "app",
            tts_provider="gpt-sovits",
            presets={
                "default": GPTSoVITSPresetConfig(
                    ref_audio_path=_REMOTE_REFERENCE,
                    prompt_text=_PRIVATE_PROMPT,
                )
            },
        )
        result = await runner.run(settings, publish=updates.append)
        await client.aclose()

        checks = {check.name: check for check in result}
        assert checks["tts_service"].state is ProviderPreflightState.ready
        assert checks["tts_preset"].state is ProviderPreflightState.ready
        assert checks["tts_reference"].state is ProviderPreflightState.ready
        assert requests[0] == {}
        assert requests[1]["text"] == "连接测试"
        assert requests[1]["ref_audio_path"] == _REMOTE_REFERENCE
        assert requests[1]["prompt_text"] == _PRIVATE_PROMPT
        assert not list(output.glob("*.wav"))
        rendered = repr(updates)
        assert _REMOTE_REFERENCE not in rendered
        assert _PRIVATE_PROMPT not in rendered
        assert "连接测试" not in rendered

    asyncio.run(scenario())


def test_bad_remote_preset_is_reported_without_reflecting_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(422, request=request)
            return httpx.Response(500, request=request, content=b"private upstream body")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        def tts_factory(_settings: Settings) -> GPTSoVITSProvider:
            return GPTSoVITSProvider(
                "http://127.0.0.1:9880",
                tmp_path / "failed-output",
                {"default": GPTSoVITSPreset(ref_audio_path=_REMOTE_REFERENCE)},
                client=client,
                max_owned_synthesis_tasks=1,
            )

        updates: list[tuple[ProviderPreflightCheck, ...]] = []
        await ProviderPreflightRunner(tts_factory=tts_factory).run(
            _settings(
                tmp_path / "app",
                tts_provider="gpt-sovits",
                presets={"default": GPTSoVITSPresetConfig(ref_audio_path=_REMOTE_REFERENCE)},
            ),
            publish=updates.append,
        )
        await client.aclose()

        checks = _final(updates)
        assert checks["tts_service"].state is ProviderPreflightState.ready
        assert checks["tts_preset"].state is ProviderPreflightState.failed
        assert checks["tts_reference"].state is ProviderPreflightState.failed
        rendered = repr(updates)
        assert _REMOTE_REFERENCE not in rendered
        assert "private upstream body" not in rendered

    asyncio.run(scenario())


def test_missing_preset_fails_closed_without_constructing_provider(tmp_path: Path) -> None:
    def unexpected_factory(_settings: Settings) -> GPTSoVITSProvider:
        raise AssertionError("provider must not be built without the selected preset")

    updates: list[tuple[ProviderPreflightCheck, ...]] = []
    asyncio.run(
        ProviderPreflightRunner(tts_factory=unexpected_factory).run(
            _settings(tmp_path, tts_provider="gpt-sovits"),
            publish=updates.append,
        )
    )
    checks = _final(updates)
    assert checks["tts_service"].state is ProviderPreflightState.skipped
    assert checks["tts_preset"].reason_code == "tts_preset_required"
    assert checks["tts_reference"].reason_code == "tts_reference_required"


class _ProbeProvider:
    def __init__(
        self,
        probe: GPTSoVITSProbe,
        *,
        probe_failure: bool = False,
        synthesis_failure: bool = False,
    ) -> None:
        self._probe = probe
        self._probe_failure = probe_failure
        self._synthesis_failure = synthesis_failure
        self.closed = False

    async def probe(self, *, timeout_ms: int) -> GPTSoVITSProbe:
        assert timeout_ms > 0
        if self._probe_failure:
            raise RuntimeError("private synthetic probe failure")
        return self._probe

    async def synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult:
        del job, segment_index, token
        if self._synthesis_failure:
            raise RuntimeError("private synthetic provider failure")
        raise AssertionError("synthesis must not run when probe is unavailable")

    async def discard(self, result: AudioResult) -> None:
        del result

    async def close(self) -> None:
        self.closed = True


def test_tts_preflight_classifies_unsupported_constructor_probe_and_synthesis_failures(
    tmp_path: Path,
) -> None:
    configured = _settings(
        tmp_path,
        tts_provider="gpt-sovits",
        presets={"default": GPTSoVITSPresetConfig(ref_audio_path=_REMOTE_REFERENCE)},
    )

    unsupported_updates: list[tuple[ProviderPreflightCheck, ...]] = []
    asyncio.run(
        ProviderPreflightRunner().run(
            _settings(tmp_path / "unsupported", tts_provider="other"),
            publish=unsupported_updates.append,
        )
    )
    assert _final(unsupported_updates)["tts_service"].reason_code == "tts_provider_unsupported"

    def invalid_factory(_settings: Settings) -> GPTSoVITSProvider:
        raise ValueError(_REMOTE_REFERENCE)

    invalid_updates: list[tuple[ProviderPreflightCheck, ...]] = []
    asyncio.run(
        ProviderPreflightRunner(tts_factory=invalid_factory).run(
            configured,
            publish=invalid_updates.append,
        )
    )
    assert _final(invalid_updates)["tts_reference"].reason_code == "tts_reference_unavailable"
    assert _REMOTE_REFERENCE not in repr(invalid_updates)

    unavailable = _ProbeProvider(
        GPTSoVITSProbe(False, None, error_code="tts_timeout"),
    )
    unavailable_updates: list[tuple[ProviderPreflightCheck, ...]] = []
    asyncio.run(
        ProviderPreflightRunner(tts_factory=lambda _settings: unavailable).run(
            configured,
            publish=unavailable_updates.append,
        )
    )
    assert _final(unavailable_updates)["tts_service"].reason_code == "tts_timeout"
    assert unavailable.closed

    probe_failure = _ProbeProvider(
        GPTSoVITSProbe(False, None),
        probe_failure=True,
    )
    probe_failure_updates: list[tuple[ProviderPreflightCheck, ...]] = []
    asyncio.run(
        ProviderPreflightRunner(tts_factory=lambda _settings: probe_failure).run(
            configured,
            publish=probe_failure_updates.append,
        )
    )
    probe_failure_checks = _final(probe_failure_updates)
    assert probe_failure_checks["tts_service"].state is ProviderPreflightState.failed
    assert probe_failure_checks["tts_preset"].state is ProviderPreflightState.skipped
    assert probe_failure.closed
    assert "private synthetic probe failure" not in repr(probe_failure_updates)

    failing = _ProbeProvider(
        GPTSoVITSProbe(True, "api_v2", status_code=422),
        synthesis_failure=True,
    )
    failure_updates: list[tuple[ProviderPreflightCheck, ...]] = []
    asyncio.run(
        ProviderPreflightRunner(tts_factory=lambda _settings: failing).run(
            configured,
            publish=failure_updates.append,
        )
    )
    assert _final(failure_updates)["tts_preset"].reason_code == "tts_preset_unavailable"
    assert failing.closed
    assert "private synthetic provider failure" not in repr(failure_updates)


class _SnapshotBridge:
    def __init__(
        self,
        listener: Callable[[VTSBridgeSnapshot], None],
        snapshots: tuple[VTSBridgeSnapshot, ...],
    ) -> None:
        self._listener = listener
        self._snapshots = snapshots
        self.closed = False

    def start(self) -> None:
        for snapshot in self._snapshots:
            self._listener(snapshot)

    async def close(self) -> None:
        self.closed = True


def test_vts_cold_start_disconnect_is_bounded_and_closes_bridge(tmp_path: Path) -> None:
    bridge: _SnapshotBridge | None = None

    def factory(
        _settings: Settings,
        listener: Callable[[VTSBridgeSnapshot], None],
    ) -> VTSPreflightBridge:
        nonlocal bridge
        bridge = _SnapshotBridge(
            listener,
            (
                _snapshot(VTSBridgeState.connecting),
                _snapshot(VTSBridgeState.authorizing, api_available=True),
                _snapshot(VTSBridgeState.backoff, error_code="vts_disconnected"),
            ),
        )
        return bridge

    updates: list[tuple[ProviderPreflightCheck, ...]] = []
    asyncio.run(
        ProviderPreflightRunner(vts_factory=factory).run(
            _settings(tmp_path, vts_enabled=True),
            publish=updates.append,
        )
    )

    checks = _final(updates)
    assert checks["vts_service"].state is ProviderPreflightState.reconnecting
    assert checks["vts_service"].reason_code == "vts_disconnected"
    assert all(
        checks[name].state is ProviderPreflightState.skipped
        for name in ("vts_authentication", "vts_model", "vts_hotkeys")
    )
    assert bridge is not None and bridge.closed
    assert any(
        next(check for check in update if check.name == "vts_authentication").state
        is ProviderPreflightState.action_required
        for update in updates
    )


def test_vts_factory_failure_is_content_free(tmp_path: Path) -> None:
    def failing_factory(
        _settings: Settings,
        _listener: Callable[[VTSBridgeSnapshot], None],
    ) -> VTSPreflightBridge:
        raise RuntimeError("private VTS failure detail")

    updates: list[tuple[ProviderPreflightCheck, ...]] = []
    asyncio.run(
        ProviderPreflightRunner(vts_factory=failing_factory).run(
            _settings(tmp_path, vts_enabled=True),
            publish=updates.append,
        )
    )
    checks = _final(updates)
    assert checks["vts_service"].reason_code == "vts_unavailable"
    assert "private VTS failure detail" not in repr(updates)


def test_vts_failure_snapshots_distinguish_revoke_model_and_hotkey() -> None:
    revoked = {
        check.name: check
        for check in _vts_checks(
            _snapshot(
                VTSBridgeState.disabled,
                error_code="vts_auth_revoked",
                api_available=True,
            )
        )
    }
    assert revoked["vts_service"].state is ProviderPreflightState.ready
    assert revoked["vts_authentication"].state is ProviderPreflightState.failed
    assert revoked["vts_model"].state is ProviderPreflightState.skipped

    missing_model = {
        check.name: check
        for check in _vts_checks(
            _snapshot(
                VTSBridgeState.disabled,
                error_code="vts_model_missing",
                api_available=True,
                authenticated=True,
            )
        )
    }
    assert missing_model["vts_authentication"].state is ProviderPreflightState.ready
    assert missing_model["vts_model"].state is ProviderPreflightState.failed
    assert missing_model["vts_hotkeys"].state is ProviderPreflightState.skipped

    missing_hotkey = {
        check.name: check
        for check in _vts_checks(
            _snapshot(
                VTSBridgeState.disabled,
                error_code="vts_hotkey_missing",
                api_available=True,
                authenticated=True,
                model_loaded=True,
                missing_hotkey_count=2,
            )
        )
    }
    assert missing_hotkey["vts_model"].state is ProviderPreflightState.ready
    assert missing_hotkey["vts_hotkeys"].state is ProviderPreflightState.failed
    assert missing_hotkey["vts_hotkeys"].missing_count == 2


def test_provider_preflight_contracts_reject_unbounded_or_duplicate_state() -> None:
    with pytest.raises(ValueError, match="stable code"):
        ProviderPreflightCheck(
            name="tts_service",
            state=ProviderPreflightState.failed,
            reason_code=_REMOTE_REFERENCE,
        )
    with pytest.raises(ValueError, match="count"):
        ProviderPreflightCheck(
            name="vts_hotkeys",
            state=ProviderPreflightState.failed,
            missing_count=257,
        )
    duplicate = ProviderPreflightCheck(
        name="tts_service",
        state=ProviderPreflightState.ready,
    )
    with pytest.raises(ValueError, match="unique"):
        ProviderPreflightEvent(checks=(duplicate, duplicate))


class _ManagementPreflight:
    async def run(
        self,
        _settings: Settings,
        *,
        publish: Callable[[tuple[ProviderPreflightCheck, ...]], None],
    ) -> tuple[ProviderPreflightCheck, ...]:
        checks = (
            ProviderPreflightCheck(
                name="tts_service",
                state=ProviderPreflightState.ready,
            ),
        )
        publish(checks)
        return checks


def test_management_dispatch_publishes_correlated_preflight_and_completion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        management = DesktopManagementRuntime(
            _settings(tmp_path),
            None,
            provider_preflight=cast(ProviderPreflightRunner, _ManagementPreflight()),
        )
        bridge = ApplicationBridge()
        command = ProviderPreflightCommand(command_id="cmd_w19_preflight")
        await management.dispatch(
            bridge,
            command,
            capabilities=BackendCapabilities(),
        )
        events = list(bridge.drain_events())
        snapshot = next(event for event in events if isinstance(event, ProviderPreflightEvent))
        assert snapshot.command_id == command.command_id
        assert snapshot.checks[0].state is ProviderPreflightState.ready
        assert any(
            isinstance(event, ManagementResultEvent)
            and event.command_id == command.command_id
            and event.operation == "provider_preflight_completed"
            for event in events
        )

    asyncio.run(scenario())


def _form() -> DesktopSettingsForm:
    return DesktopSettingsForm(
        llm_provider="mock",
        llm_base_url="https://api.example.invalid",
        llm_model="",
        tts_provider="gpt-sovits",
        tts_base_url="http://127.0.0.1:9880",
        tts_preset_name="megumin",
        tts_ref_audio_path=_REMOTE_REFERENCE,
        tts_ref_audio_scope="service_resource",
        tts_prompt_text=_PRIVATE_PROMPT,
        tts_prompt_lang="zh",
        vts_enabled=True,
        vts_uri="ws://127.0.0.1:8001",
        vts_plugin_name="Megumin Companion",
        vts_plugin_developer="Local User",
        stt_enabled=False,
        stt_executable="whisper/whisper-cli.exe",
        stt_model_path="whisper/ggml-base.bin",
        stt_device="",
        startup_enabled=False,
    )


def test_w19_settings_save_persists_default_preset_for_next_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "app")
        management = DesktopManagementRuntime(settings, None)
        bridge = ApplicationBridge()
        await management.dispatch(
            bridge,
            SettingsSaveCommand(payload=_form(), command_id="cmd_w19_save"),
            capabilities=BackendCapabilities(),
        )
        events = list(bridge.drain_events())
        assert any(
            isinstance(event, ManagementResultEvent)
            and event.operation == "settings_saved"
            and event.restart_required
            for event in events
        )
        layer = read_user_settings(app_paths=settings.paths)
        assert layer["tts"]["default_preset"] == "megumin"
        assert layer["tts"]["presets"]["megumin"] == {
            "ref_audio_path": _REMOTE_REFERENCE,
            "ref_audio_scope": "service_resource",
            "prompt_text": _PRIVATE_PROMPT,
            "prompt_lang": "zh",
            "text_lang": "zh",
        }

    asyncio.run(scenario())


class _ConfirmingSettingsDialog(SettingsDialog):
    def _confirm(self, _title: str, _message: str) -> bool:
        return True


def test_w19_dialog_requires_saved_settings_and_renders_content_free_status(
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

    dialog = _ConfirmingSettingsDialog(model, submit)

    assert "服务预检" in [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    assert dialog.tts_preset_name.text() == "megumin"
    assert dialog.tts_ref_audio_path.text() == _REMOTE_REFERENCE

    dialog._form_dirty = True
    dialog._run_provider_preflight()
    assert submitted == []
    assert "先保存设置" in dialog.status_label.text()

    dialog._form_dirty = False
    dialog._run_provider_preflight()
    assert isinstance(submitted[-1], ProviderPreflightCommand)
    assert not dialog.run_provider_preflight.isEnabled()

    model.apply_event(
        ProviderPreflightEvent(
            checks=(
                ProviderPreflightCheck(
                    name="vts_hotkeys",
                    state=ProviderPreflightState.failed,
                    reason_code="vts_hotkey_missing",
                    missing_count=2,
                ),
            )
        )
    )
    model.apply_event(
        ManagementResultEvent(
            operation="provider_preflight_completed",
            command_id=submitted[-1].command_id,
        )
    )
    dialog.sync_from_model()
    assert "缺少 2 项" in dialog.preflight_labels["vts_hotkeys"].text()
    assert _REMOTE_REFERENCE not in dialog.preflight_labels["vts_hotkeys"].text()
    assert dialog.run_provider_preflight.isEnabled()

    dialog.clear_sensitive()
    assert dialog.tts_ref_audio_path.text() == ""
    assert dialog.preflight_labels["vts_hotkeys"].text() == ""
