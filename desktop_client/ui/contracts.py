"""Typed, body-safe contracts for the in-process desktop bridge."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from app.media.types import MAX_OUTPUT_DEVICES, AudioOutputDevice
from app.memory.models import MemoryItem, MemorySensitivity, MemoryStatus, MemoryType
from app.schemas import (
    FeatureName,
    FeatureState,
    InputMode,
    PipelineEvent,
    SessionReset,
    SessionSnapshotChunk,
    TurnInterruptRequest,
    UserMessage,
    utc_now,
)
from app.schemas.messages import prefixed_id
from app.stt_runtime import (
    MANAGED_STT_PROFILE,
    SttRuntimeState,
    SttRuntimeStatus,
    managed_stt_manifest,
)

BRIDGE_PROTOCOL_VERSION: Literal[1] = 1
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_AUDIO_OUTPUT_DEVICE_ID = re.compile(r"^audio_[0-9a-f]{32}$")

MAX_MANAGEMENT_LIST_ITEMS = 20
MAX_MANAGEMENT_PREVIEW_CHARS = 512
MAX_MANAGEMENT_CONTENT_CHARS = 5_000
MAX_MANAGEMENT_PATH_CHARS = 4_096
MAX_SECRET_CHARS = 64 * 1024
MAX_PROVIDER_PREFLIGHT_CHECKS = 7


def _validate_bounded_text(
    value: str,
    *,
    field_name: str,
    maximum: int,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum:
        raise ValueError(f"{field_name} is outside the bridge bound")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_opaque_id(value: str, *, field_name: str) -> None:
    if not _OPAQUE_ID.fullmatch(value):
        raise ValueError(f"{field_name} is outside the bridge bound")


@dataclass(frozen=True, slots=True)
class UserMessageCommand:
    """Carry the existing v1 user-message payload without a network identity."""

    payload: UserMessage
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["user.message"] = field(default="user.message", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)

    @property
    def session_id(self) -> str:
        return self.payload.session_id


@dataclass(frozen=True, slots=True)
class TurnCancelCommand:
    """Carry the existing v1 cancellation payload."""

    payload: TurnInterruptRequest
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["turn.cancel"] = field(default="turn.cancel", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)

    @property
    def session_id(self) -> str:
        return self.payload.session_id


@dataclass(frozen=True, slots=True)
class VoiceStartCommand:
    """Begin explicit PTT capture; no audio payload crosses the UI bridge."""

    session_id: str = "local_session"
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["voice.start"] = field(default="voice.start", init=False)

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, field_name="voice session id")
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class VoiceStopCommand:
    """Stop PTT capture and ask MediaWorker for one bounded transcript."""

    session_id: str = "local_session"
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["voice.stop"] = field(default="voice.stop", init=False)

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, field_name="voice session id")
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class VoiceCancelCommand:
    """Fail closed when release/focus/session state makes capture unsafe."""

    session_id: str = "local_session"
    reason_code: str = "voice_released"
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["voice.cancel"] = field(default="voice.cancel", init=False)

    def __post_init__(self) -> None:
        _validate_opaque_id(self.session_id, field_name="voice session id")
        if not is_stable_reason_code(self.reason_code):
            raise ValueError("voice cancellation reason must be stable")
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class DesktopSettingsForm:
    """The small, secret-free subset of settings editable by the desktop UI."""

    llm_provider: str
    llm_base_url: str
    llm_model: str
    tts_provider: str
    tts_base_url: str
    vts_enabled: bool
    vts_uri: str
    vts_plugin_name: str
    vts_plugin_developer: str
    stt_enabled: bool
    stt_executable: str
    stt_model_path: str
    stt_device: str
    startup_enabled: bool
    output_device_id: str = ""
    system_playback_enabled: bool = False
    stt_profile: str = MANAGED_STT_PROFILE
    tts_preset_name: str = "default"
    tts_ref_audio_path: str = ""
    tts_ref_audio_scope: Literal["service_resource", "local_file"] = "service_resource"
    tts_prompt_text: str = ""
    tts_prompt_lang: str = "zh"

    def __post_init__(self) -> None:
        for field_name, value, maximum, allow_empty in (
            ("llm_provider", self.llm_provider, 128, False),
            ("llm_base_url", self.llm_base_url, 2_048, False),
            ("llm_model", self.llm_model, 512, True),
            ("tts_provider", self.tts_provider, 128, False),
            ("tts_base_url", self.tts_base_url, 2_048, False),
            ("vts_uri", self.vts_uri, 2_048, False),
            ("vts_plugin_name", self.vts_plugin_name, 128, False),
            ("vts_plugin_developer", self.vts_plugin_developer, 128, False),
            ("stt_executable", self.stt_executable, MAX_MANAGEMENT_PATH_CHARS, False),
            ("stt_model_path", self.stt_model_path, MAX_MANAGEMENT_PATH_CHARS, False),
            ("stt_device", self.stt_device, 256, True),
            ("output_device_id", self.output_device_id, 40, True),
            ("tts_preset_name", self.tts_preset_name, 128, False),
            (
                "tts_ref_audio_path",
                self.tts_ref_audio_path,
                MAX_MANAGEMENT_PATH_CHARS,
                True,
            ),
            (
                "tts_prompt_text",
                self.tts_prompt_text,
                MAX_MANAGEMENT_CONTENT_CHARS,
                True,
            ),
            ("tts_prompt_lang", self.tts_prompt_lang, 32, False),
        ):
            _validate_bounded_text(
                value,
                field_name=field_name,
                maximum=maximum,
                allow_empty=allow_empty,
            )
        if self.output_device_id and not _AUDIO_OUTPUT_DEVICE_ID.fullmatch(
            self.output_device_id.strip()
        ):
            raise ValueError("output_device_id is outside the bridge bound")
        if not isinstance(self.system_playback_enabled, bool):
            raise ValueError("system_playback_enabled is outside the bridge bound")
        if self.tts_ref_audio_scope not in {"service_resource", "local_file"}:
            raise ValueError("tts_ref_audio_scope is outside the bridge bound")
        try:
            object.__setattr__(self, "stt_profile", managed_stt_manifest(self.stt_profile).profile)
        except ValueError as exc:
            raise ValueError("stt_profile is outside the managed Whisper catalogue") from exc


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """Body-safe presentation state; encrypted secret values never appear here."""

    form: DesktopSettingsForm
    llm_secret_configured: bool
    vts_secret_configured: bool
    settings_schema_upgrade_required: bool
    stt_runtime: SttRuntimeStatus = field(
        default_factory=lambda: SttRuntimeStatus(SttRuntimeState.missing)
    )


@dataclass(frozen=True, slots=True)
class ManagementRefreshCommand:
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["management.refresh"] = field(default="management.refresh", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class SettingsSaveCommand:
    payload: DesktopSettingsForm
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["settings.save"] = field(default="settings.save", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class SttInstallCommand:
    """Explicitly install or repair the one managed local Chinese STT runtime."""

    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["stt.install"] = field(default="stt.install", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class AudioOutputDevicesCommand:
    """Request a bounded MediaWorker output-device snapshot."""

    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["audio.output_devices"] = field(default="audio.output_devices", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class ProviderPreflightCommand:
    """Explicitly probe the persisted TTS/VTS configuration on BackendThread."""

    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["provider.preflight"] = field(default="provider.preflight", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class SecretStoreCommand:
    """Write-only secret input.  No event ever echoes ``value``."""

    secret_id: Literal["llm", "vts"]
    value: str = field(repr=False)
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["secret.store"] = field(default="secret.store", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        _validate_bounded_text(
            self.value,
            field_name="secret value",
            maximum=MAX_SECRET_CHARS,
        )


@dataclass(frozen=True, slots=True)
class SecretRevokeCommand:
    secret_id: Literal["llm", "vts"]
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["secret.revoke"] = field(default="secret.revoke", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class FeatureSetCommand:
    feature: FeatureName
    enabled: bool
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["feature.set"] = field(default="feature.set", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class MemoryListCommand:
    query: str = ""
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.list"] = field(default="memory.list", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        _validate_bounded_text(
            self.query,
            field_name="memory query",
            maximum=MAX_MANAGEMENT_CONTENT_CHARS,
            allow_empty=True,
        )


@dataclass(frozen=True, slots=True)
class MemoryDetailCommand:
    memory_id: str
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.detail"] = field(default="memory.detail", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        _validate_opaque_id(self.memory_id, field_name="memory id")


@dataclass(frozen=True, slots=True)
class MemoryUpdateCommand:
    memory_id: str
    content: str
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.update"] = field(default="memory.update", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        _validate_opaque_id(self.memory_id, field_name="memory id")
        _validate_bounded_text(
            self.content,
            field_name="memory content",
            maximum=MAX_MANAGEMENT_CONTENT_CHARS,
        )


@dataclass(frozen=True, slots=True)
class MemoryDeleteCommand:
    memory_id: str
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.delete"] = field(default="memory.delete", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        _validate_opaque_id(self.memory_id, field_name="memory id")


@dataclass(frozen=True, slots=True)
class MemoryClearCommand:
    target: Literal["history", "memories"]
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.clear"] = field(default="memory.clear", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class MemoryConfirmCommand:
    confirmation_id: str
    approved: bool
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.confirm"] = field(default="memory.confirm", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        _validate_opaque_id(self.confirmation_id, field_name="confirmation id")


@dataclass(frozen=True, slots=True)
class MemoryExportCommand:
    destination: str
    overwrite: bool = False
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.export"] = field(default="memory.export", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        _validate_bounded_text(
            self.destination,
            field_name="export destination",
            maximum=MAX_MANAGEMENT_PATH_CHARS,
        )


@dataclass(frozen=True, slots=True)
class ManagementDebugCommand:
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["management.debug"] = field(default="management.debug", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)


ManagementCommand: TypeAlias = (
    ManagementRefreshCommand
    | SettingsSaveCommand
    | SttInstallCommand
    | AudioOutputDevicesCommand
    | ProviderPreflightCommand
    | SecretStoreCommand
    | SecretRevokeCommand
    | FeatureSetCommand
    | MemoryListCommand
    | MemoryDetailCommand
    | MemoryUpdateCommand
    | MemoryDeleteCommand
    | MemoryClearCommand
    | MemoryConfirmCommand
    | MemoryExportCommand
    | ManagementDebugCommand
)

BridgeCommand: TypeAlias = (
    UserMessageCommand
    | TurnCancelCommand
    | VoiceStartCommand
    | VoiceStopCommand
    | VoiceCancelCommand
    | ManagementCommand
)


class BackendState(StrEnum):
    starting = "starting"
    ready = "ready"
    restarting = "restarting"
    degraded = "degraded"
    failed = "failed"
    stopping = "stopping"
    stopped = "stopped"


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Only capabilities that the W13 shell is allowed to enable."""

    text_chat: bool = False
    turn_cancel: bool = False
    voice_input: bool = False


@dataclass(frozen=True, slots=True)
class BackendStateEvent:
    generation: int
    state: BackendState
    reason_code: str | None = None
    capabilities: BackendCapabilities = field(default_factory=BackendCapabilities)
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["backend.state"] = field(default="backend.state", init=False)

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("backend generation must be positive")
        if self.reason_code is not None and not is_stable_reason_code(self.reason_code):
            raise ValueError("backend reason must be a stable code")


@dataclass(frozen=True, slots=True)
class CommandRejectedEvent:
    command_id: str
    reason_code: str
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["command.rejected"] = field(default="command.rejected", init=False)

    def __post_init__(self) -> None:
        if not 1 <= len(self.command_id) <= 128:
            raise ValueError("command id is outside the bridge bound")
        if not is_stable_reason_code(self.reason_code):
            raise ValueError("command rejection reason must be a stable code")


class VoiceInputState(StrEnum):
    disabled = "disabled"
    idle = "idle"
    recording = "recording"
    transcribing = "transcribing"
    timed_out = "timed_out"
    failed = "failed"


@dataclass(frozen=True, slots=True)
class VoiceStateEvent:
    """Bounded PTT state only; transcript text travels directly to TurnService."""

    state: VoiceInputState
    reason_code: str | None = None
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["voice.state"] = field(default="voice.state", init=False)

    def __post_init__(self) -> None:
        if self.reason_code is not None and not is_stable_reason_code(self.reason_code):
            raise ValueError("voice state reason must be a stable code")
        if self.command_id is not None:
            _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class VoiceUserMessageEvent:
    """One completed local transcript for the visible chat surface.

    The event is deliberately limited to the existing ``UserMessage`` body.
    It contains neither PCM/WAV/JSON nor any microphone/device metadata.
    """

    message: UserMessage
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["voice.message"] = field(default="voice.message", init=False)

    def __post_init__(self) -> None:
        if self.message.input_mode is not InputMode.voice:
            raise ValueError("voice message event requires a voice UserMessage")
        if self.command_id is not None:
            _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class BridgeOverflowEvent:
    dropped_count: int
    reason_code: Literal["event_snapshot_required"] = "event_snapshot_required"
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["bridge.overflow"] = field(default="bridge.overflow", init=False)

    def __post_init__(self) -> None:
        if self.dropped_count < 1:
            raise ValueError("overflow must report at least one dropped event")


@dataclass(frozen=True, slots=True)
class SettingsSnapshotEvent:
    snapshot: SettingsSnapshot
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["settings.snapshot"] = field(default="settings.snapshot", init=False)

    def __post_init__(self) -> None:
        if self.command_id is not None:
            _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class AudioOutputDevicesEvent:
    """Bounded, body-free local-device labels for the settings selector."""

    devices: tuple[AudioOutputDevice, ...]
    truncated: bool = False
    reason_code: str | None = None
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["audio.output_devices"] = field(default="audio.output_devices", init=False)

    def __post_init__(self) -> None:
        if len(self.devices) > MAX_OUTPUT_DEVICES or not isinstance(self.truncated, bool):
            raise ValueError("audio output device batch is outside the bridge bound")
        if self.reason_code is not None and not is_stable_reason_code(self.reason_code):
            raise ValueError("audio output device reason must be a stable code")
        if self.command_id is not None:
            _validate_command_id(self.command_id)


class ProviderPreflightState(StrEnum):
    pending = "pending"
    running = "running"
    ready = "ready"
    skipped = "skipped"
    failed = "failed"
    action_required = "action_required"
    reconnecting = "reconnecting"


ProviderPreflightName: TypeAlias = Literal[
    "tts_service",
    "tts_preset",
    "tts_reference",
    "vts_service",
    "vts_authentication",
    "vts_model",
    "vts_hotkeys",
]


@dataclass(frozen=True, slots=True)
class ProviderPreflightCheck:
    """One content-free W19 capability check."""

    name: ProviderPreflightName
    state: ProviderPreflightState
    reason_code: str | None = None
    missing_count: int = 0

    def __post_init__(self) -> None:
        if self.reason_code is not None and not is_stable_reason_code(self.reason_code):
            raise ValueError("provider preflight reason must be a stable code")
        if self.missing_count < 0 or self.missing_count > 256:
            raise ValueError("provider preflight count is outside the bridge bound")


@dataclass(frozen=True, slots=True)
class ProviderPreflightEvent:
    """A complete, bounded snapshot with no provider bodies or identifiers."""

    checks: tuple[ProviderPreflightCheck, ...]
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["provider.preflight"] = field(default="provider.preflight", init=False)

    def __post_init__(self) -> None:
        if not self.checks or len(self.checks) > MAX_PROVIDER_PREFLIGHT_CHECKS:
            raise ValueError("provider preflight snapshot is outside the bridge bound")
        names = tuple(check.name for check in self.checks)
        if len(set(names)) != len(names):
            raise ValueError("provider preflight names must be unique")
        if self.command_id is not None:
            _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class FeatureStatesEvent:
    states: tuple[FeatureState, ...]
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["feature.states"] = field(default="feature.states", init=False)

    def __post_init__(self) -> None:
        if not self.states or len(self.states) > len(FeatureName):
            raise ValueError("feature state batch is outside the bridge bound")
        if self.command_id is not None:
            _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class MemorySummary:
    memory_id: str
    memory_type: MemoryType
    content_preview: str
    sensitivity: MemorySensitivity
    status: MemoryStatus
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_opaque_id(self.memory_id, field_name="memory id")
        _validate_bounded_text(
            self.content_preview,
            field_name="memory preview",
            maximum=MAX_MANAGEMENT_PREVIEW_CHARS,
        )


@dataclass(frozen=True, slots=True)
class MemoryListEvent:
    items: tuple[MemorySummary, ...]
    query: str = ""
    truncated: bool = False
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.list"] = field(default="memory.list", init=False)

    def __post_init__(self) -> None:
        if len(self.items) > MAX_MANAGEMENT_LIST_ITEMS:
            raise ValueError("memory list is outside the bridge bound")
        _validate_bounded_text(
            self.query,
            field_name="memory query",
            maximum=MAX_MANAGEMENT_CONTENT_CHARS,
            allow_empty=True,
        )
        if self.command_id is not None:
            _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class MemoryDetailEvent:
    memory_id: str
    item: MemoryItem | None
    reason_code: str | None = None
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.detail"] = field(default="memory.detail", init=False)

    def __post_init__(self) -> None:
        _validate_opaque_id(self.memory_id, field_name="memory id")
        if self.item is not None and self.item.memory_id != self.memory_id:
            raise ValueError("memory detail does not match its id")
        if self.reason_code is not None and not is_stable_reason_code(self.reason_code):
            raise ValueError("memory detail reason must be a stable code")
        if self.command_id is not None:
            _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class MemoryConfirmationSummary:
    confirmation_id: str
    content_preview: str
    evidence_preview: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _validate_opaque_id(self.confirmation_id, field_name="confirmation id")
        _validate_bounded_text(
            self.content_preview,
            field_name="confirmation preview",
            maximum=MAX_MANAGEMENT_PREVIEW_CHARS,
        )
        _validate_bounded_text(
            self.evidence_preview,
            field_name="confirmation evidence",
            maximum=MAX_MANAGEMENT_PREVIEW_CHARS,
        )


@dataclass(frozen=True, slots=True)
class MemoryConfirmationsEvent:
    items: tuple[MemoryConfirmationSummary, ...]
    truncated: bool = False
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["memory.confirmations"] = field(default="memory.confirmations", init=False)

    def __post_init__(self) -> None:
        if len(self.items) > MAX_MANAGEMENT_LIST_ITEMS:
            raise ValueError("memory confirmation list is outside the bridge bound")
        if self.command_id is not None:
            _validate_command_id(self.command_id)


@dataclass(frozen=True, slots=True)
class ManagementResultEvent:
    """One body-free terminal result for a W16 mutation or export command."""

    operation: str
    command_id: str
    reason_code: str | None = None
    deleted_count: int = 0
    cleanup_pending: bool = False
    restart_required: bool = False
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["management.result"] = field(default="management.result", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)
        if not is_stable_reason_code(self.operation):
            raise ValueError("management operation must be a stable code")
        if self.reason_code is not None and not is_stable_reason_code(self.reason_code):
            raise ValueError("management result reason must be a stable code")
        if self.deleted_count < 0:
            raise ValueError("management deleted count must not be negative")


@dataclass(frozen=True, slots=True)
class ManagementDebugEvent:
    version: str
    capabilities: BackendCapabilities
    command_queue_count: int
    command_queue_capacity: int
    event_queue_count: int
    event_queue_capacity: int
    command_id: str | None = None
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["management.debug"] = field(default="management.debug", init=False)

    def __post_init__(self) -> None:
        _validate_bounded_text(
            self.version,
            field_name="application version",
            maximum=128,
        )
        for count, capacity in (
            (self.command_queue_count, self.command_queue_capacity),
            (self.event_queue_count, self.event_queue_capacity),
        ):
            if count < 0 or capacity < 1 or count > capacity:
                raise ValueError("debug queue state is outside the bridge bound")
        if self.command_id is not None:
            _validate_command_id(self.command_id)


BridgeEvent: TypeAlias = (
    PipelineEvent
    | SessionReset
    | SessionSnapshotChunk
    | BackendStateEvent
    | CommandRejectedEvent
    | VoiceStateEvent
    | VoiceUserMessageEvent
    | BridgeOverflowEvent
    | SettingsSnapshotEvent
    | AudioOutputDevicesEvent
    | ProviderPreflightEvent
    | FeatureStatesEvent
    | MemoryListEvent
    | MemoryDetailEvent
    | MemoryConfirmationsEvent
    | ManagementResultEvent
    | ManagementDebugEvent
)


def is_stable_reason_code(value: str) -> bool:
    return _REASON_CODE.fullmatch(value) is not None


def _validate_command_id(value: str) -> None:
    if not 1 <= len(value) <= 128:
        raise ValueError("command id is outside the bridge bound")


def is_terminal_event(event: BridgeEvent) -> bool:
    if isinstance(event, BackendStateEvent):
        return event.state in {BackendState.failed, BackendState.stopped}
    if isinstance(event, CommandRejectedEvent | BridgeOverflowEvent):
        return True
    if isinstance(event, VoiceStateEvent):
        return event.state in {
            VoiceInputState.disabled,
            VoiceInputState.idle,
            VoiceInputState.timed_out,
            VoiceInputState.failed,
        }
    if isinstance(event, VoiceUserMessageEvent):
        return True
    if isinstance(
        event,
        (
            SettingsSnapshotEvent,
            AudioOutputDevicesEvent,
            ProviderPreflightEvent,
            FeatureStatesEvent,
            MemoryListEvent,
            MemoryDetailEvent,
            MemoryConfirmationsEvent,
            ManagementResultEvent,
            ManagementDebugEvent,
        ),
    ):
        return True
    return event.type in {
        "turn.completed",
        "turn.cancelled",
        "turn.failed",
        "assistant.completed",
        "assistant.output_incomplete",
    }
