"""Single-owner VTube Studio runtime for avatar motion and parameter control."""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Protocol

from app.avatar.controller import AvatarParameterController, RandomSource
from app.avatar.models import (
    AvatarHealthSnapshot,
    AvatarParameterFrame,
    AvatarRuntimeState,
    AvatarTurnPlan,
)
from app.avatar.red_eye import RedEyeCommand, RedEyeOwner, RedEyeOwnership
from app.clients.vts import (
    TokenStore,
    VTSAPIError,
    VTSAuthenticationError,
    VTSConfigurationError,
    VTSConnectionError,
    VTSEvent,
    VTSHotkeyTriggeredEvent,
    VTSModelLoadedEvent,
    VTSParameterCapability,
    VTSProtocolError,
    VTSRequestTimeout,
    VTSToken,
)
from app.config.settings import AvatarConfig
from app.emotion import EmotionLabel
from app.health import CapabilityCheck, CapabilityState
from app.media import MouthEnvelopeSample
from app.secret_store import SecretStoreError


class AvatarClient(Protocol):
    """The VTS operations owned by exactly one runtime writer."""

    @property
    def dropped_event_count(self) -> int: ...

    async def connect(self) -> None: ...

    async def api_state(self) -> dict[str, Any]: ...

    async def request_token(self, plugin_name: str, plugin_developer: str) -> str: ...

    async def authenticate(
        self,
        plugin_name: str,
        plugin_developer: str,
        authentication_token: str,
    ) -> bool: ...

    async def current_model_name(self) -> str: ...

    async def parameter_capabilities(self) -> dict[str, VTSParameterCapability]: ...

    async def resolve_hotkey_name(self, name: str) -> str: ...

    async def subscribe_event(self, event_name: str, *, subscribe: bool = True) -> None: ...

    async def inject_parameter_values(
        self,
        values: Mapping[str, float],
        *,
        face_found: bool,
    ) -> None: ...

    async def expression_is_active(self, expression_file: str) -> bool: ...

    async def set_expression_active(
        self,
        expression_file: str,
        *,
        active: bool,
        fade_seconds: float,
    ) -> None: ...

    async def trigger_hotkey(self, hotkey_id: str) -> None: ...

    async def next_event(self) -> VTSEvent: ...

    async def wait_closed(self) -> None: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[], AvatarClient]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
StateListener = Callable[[AvatarHealthSnapshot], None]

_INTENTIONAL_LAYER_DISABLE_CODES = frozenset(
    {
        "avatar_parameter_disabled",
        "avatar_lip_sync_disabled",
        "avatar_body_disabled",
        "avatar_red_eye_disabled",
    }
)


class _ActionKind(StrEnum):
    mouth_zero = "mouth_zero"
    release = "release"
    body_transition = "body_transition"
    red_eye_activate = "red_eye_activate"
    red_eye_deactivate = "red_eye_deactivate"
    red_eye_reconcile = "red_eye_reconcile"
    event_overflow = "event_overflow"
    model_loaded = "model_loaded"


class _Priority(IntEnum):
    reset = 0
    safety = 1
    effect = 2
    body = 3


@dataclass(frozen=True, slots=True)
class _Action:
    kind: _ActionKind
    priority: _Priority
    sequence: int
    lifecycle_generation: int
    vts_generation: int
    model_generation: int
    payload: object | None = None


@dataclass(frozen=True, slots=True)
class _BodyTransition:
    emotion: EmotionLabel
    motion_hotkey_id: str | None
    release_first: bool


class AvatarRuntime:
    """Own one VTS connection, one request writer, and all avatar state."""

    name = "avatar"
    required_for_readiness = False

    def __init__(
        self,
        client_factory: ClientFactory,
        token_store: TokenStore,
        *,
        plugin_name: str,
        plugin_developer: str,
        config: AvatarConfig,
        rng: RandomSource | None = None,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        state_listener: StateListener | None = None,
    ) -> None:
        if not plugin_name.strip() or not plugin_developer.strip():
            raise ValueError("Avatar VTS plugin identity is invalid")
        if (
            isinstance(reconnect_initial_seconds, bool)
            or not isinstance(reconnect_initial_seconds, (int, float))
            or not math.isfinite(reconnect_initial_seconds)
            or reconnect_initial_seconds <= 0.0
            or isinstance(reconnect_max_seconds, bool)
            or not isinstance(reconnect_max_seconds, (int, float))
            or not math.isfinite(reconnect_max_seconds)
            or reconnect_max_seconds < reconnect_initial_seconds
        ):
            raise ValueError("Avatar reconnect bounds are invalid")
        self._client_factory = client_factory
        self._token_store = token_store
        self._plugin_name = plugin_name
        self._plugin_developer = plugin_developer
        self._config = config
        self._rng = rng or random.Random()
        self._clock = clock or time.monotonic
        self._sleep = sleep
        self._reconnect_initial = float(reconnect_initial_seconds)
        self._reconnect_max = float(reconnect_max_seconds)
        self._state_listener = state_listener

        self._state = AvatarRuntimeState.stopped
        self._error_code: str | None = None
        self._parameter_error_code: str | None = None
        self._lip_sync_error_code: str | None = None
        self._body_motion_error_code: str | None = None
        self._red_eye_error_code: str | None = None
        self._parameter_control_available = False
        self._lip_sync_available = False
        self._body_motion_available = False
        self._automatic_red_eye_available = False

        self._sent_frames = 0
        self._coalesced_frames = 0
        self._dropped_actions = 0
        self._reconnect_count = 0

        self._vts_generation = 0
        self._model_generation = 0
        self._lifecycle_generation = 0
        self._turn_generation = 0
        self._current_turn_id: str | None = None
        self._current_plan: AvatarTurnPlan | None = None
        self._visual_requested = False
        self._playback_armed = False
        self._playback_job_id: str | None = None
        self._playback_sequence = 0
        self._pending_visual = False

        self._semantic_emotion = EmotionLabel.neutral
        self._body_lifecycle_emotion = EmotionLabel.neutral
        self._body_pose_active = False
        self._body_replay_required = False
        self._last_motion_by_semantic: dict[str, str] = {}

        self._current_model_name: str | None = None
        self._capabilities: dict[str, VTSParameterCapability] = {}
        self._motion_hotkey_ids: dict[str, tuple[str, ...]] = {}
        self._release_hotkey_id: str | None = None
        self._red_eye_hotkey_id: str | None = None
        self._controller: AvatarParameterController | None = None
        self._red_eye = RedEyeOwnership(duration_seconds=self._config.red_eye_duration_seconds)
        self._observed_dropped_events = 0

        self._actions: list[_Action] = []
        self._action_sequence = 0
        self._latest_frame: AvatarParameterFrame | None = None
        self._frame_in_flight = False
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active_client: AvatarClient | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._ever_ready = False

    @property
    def red_eye_owner(self) -> RedEyeOwner:
        return self._red_eye.owner

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Avatar runtime is closed")
        if not self._config.enabled:
            self._set_state(AvatarRuntimeState.disabled, "avatar_disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="avatar-runtime")

    def begin_turn(self, turn_id: str) -> int | None:
        if (
            self._closed
            or self._state is AvatarRuntimeState.disabled
            or not _valid_turn_id(turn_id)
        ):
            return None
        self._turn_generation += 1
        self._current_turn_id = turn_id
        self._current_plan = None
        self._visual_requested = False
        self._playback_armed = False
        self._playback_job_id = None
        self._playback_sequence = 0
        self._pending_visual = False
        return self._turn_generation

    def set_turn_plan(self, plan: AvatarTurnPlan, *, generation: int) -> bool:
        if (
            self._closed
            or not isinstance(plan, AvatarTurnPlan)
            or not self._matches_turn(plan.turn_id, generation)
            or self._current_plan is not None
        ):
            return False
        self._current_plan = plan
        self._semantic_emotion = plan.emotion
        controller = self._controller
        if controller is not None:
            with suppress(ValueError):
                controller.set_emotion(plan.emotion, now=self._now())
        return True

    def arm_playback(self, turn_id: str, *, generation: int) -> bool:
        if (
            self._closed
            or not self._matches_turn(turn_id, generation)
            or self._current_plan is None
        ):
            return False
        self._playback_armed = True
        self._playback_job_id = None
        self._playback_sequence = 0
        return True

    def offer_mouth_envelope(self, sample: MouthEnvelopeSample) -> bool:
        if (
            self._closed
            or not isinstance(sample, MouthEnvelopeSample)
            or sample.turn_id != self._current_turn_id
            or not self._playback_armed
            or self._current_plan is None
        ):
            return False
        if self._playback_job_id is None:
            self._playback_job_id = sample.playback_job_id
        if (
            sample.playback_job_id != self._playback_job_id
            or sample.sequence <= self._playback_sequence
        ):
            return False
        self._playback_sequence = sample.sequence
        controller = self._controller
        if controller is not None:
            controller.set_mouth(0.0 if sample.terminal else sample.value)
            controller.set_speaking(not sample.terminal)
        if sample.terminal:
            self._playback_armed = False
            self._wake.set()
            return True
        if not self._visual_requested:
            self._visual_requested = True
            self._schedule_or_defer_visual()
        self._wake.set()
        return True

    def visual_fallback(self, turn_id: str, *, generation: int) -> bool:
        if (
            self._closed
            or not self._matches_turn(turn_id, generation)
            or self._current_plan is None
        ):
            return False
        controller = self._controller
        if controller is not None:
            controller.set_mouth(0.0)
            controller.set_speaking(False)
        self._playback_armed = False
        if self._visual_requested:
            return True
        self._visual_requested = True
        self._schedule_or_defer_visual()
        return True

    def complete_turn(self, turn_id: str, *, generation: int) -> bool:
        if self._closed or not self._matches_turn(turn_id, generation):
            return False
        controller = self._controller
        if controller is not None:
            controller.set_mouth(0.0)
            controller.set_speaking(False)
        self._clear_turn()
        self._wake.set()
        return True

    def cancel_turn(self, turn_id: str, *, generation: int) -> bool:
        if self._closed or not self._matches_turn(turn_id, generation):
            return False
        controller = self._controller
        if controller is not None:
            controller.set_mouth(0.0)
            controller.set_speaking(False)
        self._lifecycle_generation += 1
        self._purge_stale_work()
        self._body_pose_active = False
        self._body_replay_required = self._semantic_emotion is not EmotionLabel.neutral
        self._queue_action(_ActionKind.mouth_zero, _Priority.reset)
        self._queue_action(_ActionKind.release, _Priority.safety)
        self._clear_turn()
        return True

    def trigger_red_eye(self) -> bool:
        if (
            self._closed
            or self._state is not AvatarRuntimeState.ready
            or not self._automatic_red_eye_available
            or self._vts_generation < 1
            or self._model_generation < 1
        ):
            return False
        command = self._red_eye.trigger_system(
            now=self._now(),
            vts_generation=self._vts_generation,
            model_generation=self._model_generation,
        )
        if command is RedEyeCommand.activate:
            self._queue_action(_ActionKind.red_eye_activate, _Priority.effect)
        else:
            self._wake.set()
        return True

    def snapshot(self) -> AvatarHealthSnapshot:
        return AvatarHealthSnapshot(
            state=self._state,
            parameter_control_available=self._parameter_control_available,
            lip_sync_available=self._lip_sync_available,
            body_motion_available=self._body_motion_available,
            automatic_red_eye_available=self._automatic_red_eye_available,
            sent_frames=self._sent_frames,
            coalesced_frames=self._coalesced_frames,
            dropped_actions=self._dropped_actions,
            reconnect_count=self._reconnect_count,
            error_code=self._error_code,
            parameter_error_code=self._parameter_error_code,
            lip_sync_error_code=self._lip_sync_error_code,
            body_motion_error_code=self._body_motion_error_code,
            red_eye_error_code=self._red_eye_error_code,
        )

    async def check_health(self) -> CapabilityCheck:
        snapshot = self.snapshot()
        if snapshot.state is AvatarRuntimeState.disabled:
            return CapabilityCheck(
                status=CapabilityState.disabled,
                error_code=snapshot.error_code,
            )
        if snapshot.state is AvatarRuntimeState.ready:
            layer_errors = (
                snapshot.parameter_error_code,
                snapshot.lip_sync_error_code,
                snapshot.body_motion_error_code,
                snapshot.red_eye_error_code,
            )
            operational_errors = tuple(
                code
                for code in layer_errors
                if code is not None and code not in _INTENTIONAL_LAYER_DISABLE_CODES
            )
            return CapabilityCheck(
                status=(CapabilityState.degraded if operational_errors else CapabilityState.ready),
                error_code=operational_errors[0] if operational_errors else None,
            )
        if snapshot.state is AvatarRuntimeState.stopped:
            return CapabilityCheck(status=CapabilityState.disabled)
        return CapabilityCheck(
            status=CapabilityState.unavailable,
            error_code=snapshot.error_code or "avatar_not_ready",
        )

    async def close(self) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(self._close_once(), name="avatar-runtime-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close_once(self) -> None:
        self._lifecycle_generation += 1
        self._purge_stale_work()
        controller = self._controller
        if controller is not None:
            controller.set_mouth(0.0)
            controller.set_speaking(False)
        self._stop.set()
        self._wake.set()
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._set_state(AvatarRuntimeState.stopped)

    async def _run(self) -> None:
        retry_attempt = 0
        try:
            while not self._stop.is_set():
                client: AvatarClient | None = None
                try:
                    client = self._client_factory()
                    self._active_client = client
                    self._set_state(AvatarRuntimeState.connecting)
                    await client.connect()
                    self._set_state(AvatarRuntimeState.authorizing)
                    await self._authorize(client)
                    self._set_state(AvatarRuntimeState.preparing)
                    self._vts_generation += 1
                    self._model_generation += 1
                    await self._prepare_model(client)
                    self._set_state(AvatarRuntimeState.ready)
                    self._ever_ready = True
                    retry_attempt = 0
                    if self._pending_visual:
                        self._pending_visual = False
                        self._schedule_or_defer_visual()
                    await self._serve_ready(client)
                    if not self._stop.is_set():
                        raise VTSConnectionError
                except asyncio.CancelledError:
                    raise
                except (
                    VTSAuthenticationError,
                    VTSConfigurationError,
                    VTSProtocolError,
                    VTSAPIError,
                    SecretStoreError,
                    ValueError,
                ) as exc:
                    if self._stop.is_set():
                        break
                    self._set_state(AvatarRuntimeState.disabled, _error_code(exc))
                    break
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    self._reconnect_count += 1
                    if client is not None:
                        await self._cleanup_session(client)
                    if self._ever_ready:
                        self._retire_connection()
                    self._set_state(AvatarRuntimeState.backoff, _error_code(exc))
                    delay = self._retry_delay(retry_attempt)
                    retry_attempt += 1
                    if client is not None:
                        await _safe_close(client)
                        if self._active_client is client:
                            self._active_client = None
                        client = None
                    if await self._wait_for_stop(delay):
                        break
                finally:
                    if client is not None:
                        await self._cleanup_session(client)
                        await _safe_close(client)
                    if self._active_client is client:
                        self._active_client = None
        finally:
            if self._stop.is_set():
                self._set_state(AvatarRuntimeState.stopped)

    async def _authorize(self, client: AvatarClient) -> None:
        state = await client.api_state()
        active = state.get("active")
        authenticated_state = state.get("currentSessionAuthenticated")
        if not isinstance(active, bool) or not isinstance(authenticated_state, bool):
            raise VTSProtocolError
        if not active:
            raise VTSConfigurationError("vts_api_unavailable")

        stored = await self._token_store.load()
        if stored is not None and (
            stored.plugin_name != self._plugin_name
            or stored.plugin_developer != self._plugin_developer
        ):
            await self._token_store.delete()
            stored = None
        if stored is not None:
            try:
                authenticated = await client.authenticate(
                    self._plugin_name,
                    self._plugin_developer,
                    stored.authentication_token,
                )
            except VTSRequestTimeout as exc:
                raise VTSAuthenticationError from exc
            if authenticated:
                return
            await self._token_store.delete()

        try:
            token = await client.request_token(
                self._plugin_name,
                self._plugin_developer,
            )
            authenticated = await client.authenticate(
                self._plugin_name,
                self._plugin_developer,
                token,
            )
        except (VTSAPIError, VTSRequestTimeout) as exc:
            raise VTSAuthenticationError from exc
        if not authenticated:
            raise VTSAuthenticationError
        await self._token_store.save(
            VTSToken(
                plugin_name=self._plugin_name,
                plugin_developer=self._plugin_developer,
                authentication_token=token,
            )
        )

    async def _prepare_model(self, client: AvatarClient) -> None:
        self._reset_layers()
        self._current_model_name = await client.current_model_name()
        await client.subscribe_event("ModelLoadedEvent")

        capabilities: dict[str, VTSParameterCapability] = {}
        if self._config.parameter_control_enabled:
            try:
                capabilities = await client.parameter_capabilities()
            except VTSConnectionError:
                raise
            except (VTSAPIError, VTSProtocolError, VTSRequestTimeout):
                self._parameter_error_code = "avatar_parameter_unavailable"
        else:
            self._parameter_error_code = "avatar_parameter_disabled"
        self._capabilities = capabilities
        self._parameter_control_available = bool(
            self._config.parameter_control_enabled and capabilities
        )
        if self._config.parameter_control_enabled and not capabilities:
            self._parameter_error_code = (
                self._parameter_error_code or "avatar_parameter_unavailable"
            )
        self._lip_sync_available = bool(
            self._parameter_control_available
            and self._config.lip_sync_enabled
            and "MouthOpen" in capabilities
        )
        if not self._config.lip_sync_enabled:
            self._lip_sync_error_code = "avatar_lip_sync_disabled"
        elif not self._config.parameter_control_enabled:
            self._lip_sync_error_code = "avatar_parameter_disabled"
        elif not self._lip_sync_available:
            self._lip_sync_error_code = "avatar_mouth_parameter_missing"
        self._controller = (
            AvatarParameterController(self._config, capabilities, rng=self._rng)
            if self._parameter_control_available
            else None
        )
        if self._controller is not None:
            self._controller.set_emotion(self._semantic_emotion, now=self._now())

        await self._prepare_body_layer(client)
        await self._prepare_red_eye_layer(client)
        await self._cleanup_session(client)
        self._body_pose_active = False
        self._body_replay_required = self._body_lifecycle_emotion is not EmotionLabel.neutral
        self._observed_dropped_events = client.dropped_event_count
        self._notify()

    async def _prepare_body_layer(self, client: AvatarClient) -> None:
        if not self._config.body_motion_enabled:
            self._body_motion_error_code = "avatar_body_disabled"
            return
        if not self._config.release_hotkey_name:
            self._body_motion_error_code = "avatar_release_unconfigured"
            return
        try:
            self._release_hotkey_id = await client.resolve_hotkey_name(
                self._config.release_hotkey_name
            )
        except VTSConnectionError:
            raise
        except (
            KeyError,
            VTSAPIError,
            VTSConfigurationError,
            VTSProtocolError,
            VTSRequestTimeout,
        ):
            self._body_motion_error_code = "avatar_release_unavailable"
            return

        selected_mapping = self._config.body_motion_hotkeys
        model_name = self._current_model_name
        if model_name is not None:
            selected_mapping = self._config.model_motion_hotkeys.get(
                model_name,
                selected_mapping,
            )
        resolved: dict[str, tuple[str, ...]] = {}
        mapping_error = False
        for semantic, candidates in selected_mapping.items():
            ids: list[str] = []
            for candidate in candidates:
                try:
                    ids.append(await client.resolve_hotkey_name(candidate))
                except VTSConnectionError:
                    raise
                except (
                    KeyError,
                    VTSAPIError,
                    VTSConfigurationError,
                    VTSProtocolError,
                    VTSRequestTimeout,
                ):
                    mapping_error = True
                    ids = []
                    break
            if ids:
                resolved[semantic] = tuple(ids)
        self._motion_hotkey_ids = resolved
        self._body_motion_available = True
        if mapping_error:
            self._body_motion_error_code = "avatar_motion_mapping_partial"

    async def _prepare_red_eye_layer(self, client: AvatarClient) -> None:
        if not self._config.auto_red_eye_enabled:
            self._red_eye_error_code = "avatar_red_eye_disabled"
            return
        if not self._config.red_eye_hotkey_name or not self._config.red_eye_expression_file:
            self._red_eye_error_code = "avatar_red_eye_unconfigured"
            return
        try:
            self._red_eye_hotkey_id = await client.resolve_hotkey_name(
                self._config.red_eye_hotkey_name
            )
            await client.subscribe_event("HotkeyTriggeredEvent")
            await client.expression_is_active(self._config.red_eye_expression_file)
        except VTSConnectionError:
            raise
        except (
            KeyError,
            VTSAPIError,
            VTSConfigurationError,
            VTSProtocolError,
            VTSRequestTimeout,
        ):
            self._red_eye_error_code = "avatar_red_eye_unavailable"
            self._automatic_red_eye_available = False
            return
        self._red_eye.reset()
        self._automatic_red_eye_available = True

    async def _serve_ready(self, client: AvatarClient) -> None:
        event_task = asyncio.create_task(
            self._event_reader(client),
            name="avatar-vts-events",
        )
        producer_task = asyncio.create_task(
            self._frame_producer(client),
            name="avatar-frame-producer",
        )
        disconnected_task = asyncio.create_task(
            client.wait_closed(),
            name="avatar-vts-disconnected",
        )
        stopped_task = asyncio.create_task(
            self._stop.wait(),
            name="avatar-runtime-stopped",
        )
        persistent = {
            event_task,
            producer_task,
            disconnected_task,
            stopped_task,
        }
        try:
            while not self._stop.is_set():
                if event_task.done():
                    event_task.result()
                    raise VTSConnectionError
                if producer_task.done():
                    producer_task.result()
                    raise VTSConnectionError
                if disconnected_task.done():
                    disconnected_task.result()
                    raise VTSConnectionError

                action = self._pop_action()
                if action is not None:
                    await self._process_action(client, action)
                    continue
                frame = self._take_latest_frame()
                if frame is not None:
                    await self._send_frame(client, frame)
                    continue

                self._wake.clear()
                if self._actions or self._latest_frame is not None:
                    continue
                wake_task = asyncio.create_task(
                    self._wake.wait(),
                    name="avatar-runtime-wake",
                )
                try:
                    done, _pending = await asyncio.wait(
                        {*persistent, wake_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopped_task in done:
                        return
                finally:
                    if not wake_task.done():
                        wake_task.cancel()
                    await asyncio.gather(wake_task, return_exceptions=True)
        finally:
            for task in persistent:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*persistent, return_exceptions=True)

    async def _event_reader(self, client: AvatarClient) -> None:
        while not self._stop.is_set():
            event = await client.next_event()
            if isinstance(event, VTSModelLoadedEvent):
                self._queue_action(
                    _ActionKind.model_loaded,
                    _Priority.reset,
                    payload=event.model_loaded,
                )
                continue
            if (
                isinstance(event, VTSHotkeyTriggeredEvent)
                and not event.triggered_by_api
                and self._config.red_eye_hotkey_name
                and event.hotkey_name.strip().casefold()
                == self._config.red_eye_hotkey_name.casefold()
            ):
                self._queue_action(
                    _ActionKind.red_eye_reconcile,
                    _Priority.safety,
                )

    async def _frame_producer(self, client: AvatarClient) -> None:
        period = min(
            1.0 / self._config.tick_hz,
            self._config.keepalive_seconds,
        )
        while not self._stop.is_set():
            controller = self._controller
            if controller is not None:
                frame = controller.compose(
                    now=self._now(),
                    vts_generation=self._vts_generation,
                    model_generation=self._model_generation,
                )
                if frame is not None:
                    self._offer_latest_frame(frame)

            if (
                self._automatic_red_eye_available
                and client.dropped_event_count > self._observed_dropped_events
            ):
                self._observed_dropped_events = client.dropped_event_count
                self._queue_action(_ActionKind.event_overflow, _Priority.reset)
            if self._automatic_red_eye_available:
                command = self._red_eye.due(
                    now=self._now(),
                    vts_generation=self._vts_generation,
                    model_generation=self._model_generation,
                )
                if command is RedEyeCommand.deactivate:
                    self._queue_action(
                        _ActionKind.red_eye_deactivate,
                        _Priority.safety,
                    )
            await self._sleep(period)

    async def _process_action(self, client: AvatarClient, action: _Action) -> None:
        if action.kind is _ActionKind.model_loaded:
            await self._handle_model_event(client, action.payload is True)
            return
        if (
            action.lifecycle_generation != self._lifecycle_generation
            or action.vts_generation != self._vts_generation
            or action.model_generation != self._model_generation
        ):
            return
        if action.kind is _ActionKind.mouth_zero:
            await self._send_mouth_zero(client)
            return
        if action.kind is _ActionKind.release:
            await self._send_release(client)
            return
        if action.kind is _ActionKind.body_transition:
            assert isinstance(action.payload, _BodyTransition)
            await self._send_body_transition(client, action.payload)
            return
        if action.kind is _ActionKind.red_eye_activate:
            await self._set_red_eye(client, active=True)
            return
        if action.kind is _ActionKind.red_eye_deactivate:
            await self._set_red_eye(client, active=False)
            return
        if action.kind is _ActionKind.red_eye_reconcile:
            await self._reconcile_red_eye(client)
            return
        if action.kind is _ActionKind.event_overflow:
            self._automatic_red_eye_available = False
            self._red_eye_error_code = "avatar_event_overflow"
            try:
                active = await client.expression_is_active(self._config.red_eye_expression_file)
            except VTSConnectionError:
                raise
            except (
                VTSAPIError,
                VTSConfigurationError,
                VTSProtocolError,
                VTSRequestTimeout,
            ):
                # Event loss makes ownership unknowable. Conservatively relinquish
                # automatic ownership without closing a possibly manual expression.
                active = True
            self._red_eye.reconcile_manual(active=active)
            self._notify()

    async def _handle_model_event(
        self,
        client: AvatarClient,
        model_loaded: bool,
    ) -> None:
        self._set_state(AvatarRuntimeState.preparing)
        self._lifecycle_generation += 1
        self._model_generation += 1
        self._retire_turn_for_reset()
        self._purge_stale_work()
        self._red_eye.reset()
        await self._cleanup_session(client)
        if not model_loaded:
            self._reset_layers()
            self._current_model_name = None
            self._set_state(AvatarRuntimeState.preparing, "vts_model_missing")
            return
        await self._prepare_model(client)
        self._set_state(AvatarRuntimeState.ready)

    async def _send_frame(
        self,
        client: AvatarClient,
        frame: AvatarParameterFrame,
    ) -> None:
        if (
            frame.vts_generation != self._vts_generation
            or frame.model_generation != self._model_generation
            or not self._parameter_control_available
        ):
            return
        try:
            self._frame_in_flight = True
            await client.inject_parameter_values(
                dict(frame.parameters),
                face_found=True,
            )
        except VTSConnectionError:
            raise
        except (VTSAPIError, VTSProtocolError, VTSRequestTimeout):
            self._disable_parameter_layer("avatar_parameter_write_failed")
            return
        finally:
            self._frame_in_flight = False
        self._sent_frames += 1
        self._notify()

    async def _send_mouth_zero(self, client: AvatarClient) -> None:
        controller = self._controller
        if controller is None:
            return
        values = controller.zero_mouth_parameters()
        if not values:
            return
        try:
            await client.inject_parameter_values(values, face_found=True)
        except VTSConnectionError:
            raise
        except (VTSAPIError, VTSProtocolError, VTSRequestTimeout):
            self._lip_sync_available = False
            self._lip_sync_error_code = "avatar_mouth_write_failed"
            self._notify()

    async def _send_release(self, client: AvatarClient) -> bool:
        hotkey_id = self._release_hotkey_id
        if not self._body_motion_available or hotkey_id is None:
            return False
        try:
            await client.trigger_hotkey(hotkey_id)
        except VTSConnectionError:
            raise
        except (VTSAPIError, VTSProtocolError, VTSRequestTimeout):
            self._disable_body_layer("avatar_release_failed")
            return False
        return True

    async def _send_body_transition(
        self,
        client: AvatarClient,
        transition: _BodyTransition,
    ) -> None:
        if not self._body_motion_available:
            return
        if transition.release_first and not await self._send_release(client):
            return
        hotkey_id = transition.motion_hotkey_id
        if hotkey_id is None:
            return
        try:
            await client.trigger_hotkey(hotkey_id)
        except VTSConnectionError:
            raise
        except (VTSAPIError, VTSProtocolError, VTSRequestTimeout):
            self._disable_body_layer("avatar_motion_failed")

    async def _set_red_eye(self, client: AvatarClient, *, active: bool) -> None:
        if not self._config.red_eye_expression_file:
            return
        try:
            await client.set_expression_active(
                self._config.red_eye_expression_file,
                active=active,
                fade_seconds=self._config.red_eye_fade_seconds,
            )
        except VTSConnectionError:
            raise
        except (VTSAPIError, VTSConfigurationError, VTSProtocolError, VTSRequestTimeout):
            self._automatic_red_eye_available = False
            self._red_eye_error_code = "avatar_red_eye_write_failed"
            self._red_eye.reset()
            self._notify()

    async def _reconcile_red_eye(self, client: AvatarClient) -> None:
        if not self._automatic_red_eye_available:
            return
        try:
            active = await client.expression_is_active(self._config.red_eye_expression_file)
        except VTSConnectionError:
            raise
        except (VTSAPIError, VTSConfigurationError, VTSProtocolError, VTSRequestTimeout):
            self._automatic_red_eye_available = False
            self._red_eye_error_code = "avatar_red_eye_state_failed"
            # Without an authoritative state result, automatic code must not risk
            # closing a red eye that the user now owns through the manual hotkey.
            self._red_eye.reconcile_manual(active=True)
            self._notify()
            return
        self._red_eye.reconcile_manual(active=active)
        self._notify()

    async def _cleanup_session(self, client: AvatarClient) -> None:
        controller = self._controller
        if controller is not None:
            controller.set_mouth(0.0)
            controller.set_speaking(False)
            values = controller.zero_mouth_parameters()
            if values:
                with suppress(Exception):
                    await client.inject_parameter_values(values, face_found=True)
        if self._release_hotkey_id is not None:
            with suppress(Exception):
                await client.trigger_hotkey(self._release_hotkey_id)
        if self._config.red_eye_expression_file:
            with suppress(Exception):
                await client.set_expression_active(
                    self._config.red_eye_expression_file,
                    active=False,
                    fade_seconds=self._config.red_eye_fade_seconds,
                )
        self._red_eye.reset()

    def _schedule_or_defer_visual(self) -> None:
        plan = self._current_plan
        if plan is None:
            return
        if (
            self._state is not AvatarRuntimeState.ready
            or self._model_generation < 1
            or self._vts_generation < 1
        ):
            self._pending_visual = True
            return
        self._pending_visual = False
        self._schedule_body_plan(plan)

    def _schedule_body_plan(self, plan: AvatarTurnPlan) -> None:
        if not self._body_motion_available:
            return
        emotion = plan.emotion
        if emotion is EmotionLabel.neutral or plan.body_motion_key is None:
            should_release = (
                self._body_pose_active
                or self._body_lifecycle_emotion is not EmotionLabel.neutral
                or self._body_replay_required
            )
            self._body_lifecycle_emotion = EmotionLabel.neutral
            self._body_pose_active = False
            self._body_replay_required = False
            if should_release:
                self._queue_action(_ActionKind.release, _Priority.safety)
            return

        same_active = (
            self._body_lifecycle_emotion is emotion
            and self._body_pose_active
            and not self._body_replay_required
        )
        if same_active:
            return
        candidates = self._motion_hotkey_ids.get(plan.body_motion_key, ())
        release_first = self._body_pose_active and self._body_lifecycle_emotion is not emotion
        selected = self._choose_motion(plan.body_motion_key, candidates)
        if not candidates:
            self._body_motion_error_code = "avatar_motion_unmapped"
            if release_first:
                self._queue_action(_ActionKind.release, _Priority.safety)
            self._body_lifecycle_emotion = emotion
            self._body_pose_active = False
            self._body_replay_required = True
            self._notify()
            return
        self._body_lifecycle_emotion = emotion
        self._body_pose_active = True
        self._body_replay_required = False
        self._queue_action(
            _ActionKind.body_transition,
            _Priority.body,
            payload=_BodyTransition(
                emotion=emotion,
                motion_hotkey_id=selected,
                release_first=release_first,
            ),
        )

    def _choose_motion(self, semantic: str, candidates: tuple[str, ...]) -> str | None:
        if not candidates:
            return None
        previous = self._last_motion_by_semantic.get(semantic)
        selectable = tuple(
            candidate for candidate in candidates if len(candidates) == 1 or candidate != previous
        )
        selected = selectable[min(len(selectable) - 1, int(self._rng.random() * len(selectable)))]
        self._last_motion_by_semantic[semantic] = selected
        return selected

    def _queue_action(
        self,
        kind: _ActionKind,
        priority: _Priority,
        *,
        payload: object | None = None,
    ) -> bool:
        if self._closed:
            return False
        self._action_sequence += 1
        action = _Action(
            kind=kind,
            priority=priority,
            sequence=self._action_sequence,
            lifecycle_generation=self._lifecycle_generation,
            vts_generation=self._vts_generation,
            model_generation=self._model_generation,
            payload=payload,
        )
        capacity = self._config.urgent_queue_capacity
        if len(self._actions) >= capacity:
            worst_index = max(
                range(len(self._actions)),
                key=lambda index: (
                    self._actions[index].priority,
                    self._actions[index].sequence,
                ),
            )
            worst = self._actions[worst_index]
            if action.priority > worst.priority:
                self._dropped_actions += 1
                self._notify()
                return False
            self._actions.pop(worst_index)
            self._dropped_actions += 1
        self._actions.append(action)
        self._wake.set()
        self._notify()
        return True

    def _pop_action(self) -> _Action | None:
        if not self._actions:
            return None
        index = min(
            range(len(self._actions)),
            key=lambda selected: (
                self._actions[selected].priority,
                self._actions[selected].sequence,
            ),
        )
        return self._actions.pop(index)

    def _offer_latest_frame(self, frame: AvatarParameterFrame) -> None:
        if self._latest_frame is not None or self._frame_in_flight:
            self._coalesced_frames += 1
        self._latest_frame = frame
        self._wake.set()

    def _take_latest_frame(self) -> AvatarParameterFrame | None:
        frame = self._latest_frame
        self._latest_frame = None
        return frame

    def _matches_turn(self, turn_id: str, generation: int) -> bool:
        return (
            not isinstance(generation, bool)
            and isinstance(generation, int)
            and generation == self._turn_generation
            and turn_id == self._current_turn_id
        )

    def _clear_turn(self) -> None:
        self._current_turn_id = None
        self._current_plan = None
        self._visual_requested = False
        self._playback_armed = False
        self._playback_job_id = None
        self._playback_sequence = 0
        self._pending_visual = False

    def _retire_turn_for_reset(self) -> None:
        self._clear_turn()
        self._body_pose_active = False
        self._body_replay_required = self._semantic_emotion is not EmotionLabel.neutral

    def _retire_connection(self) -> None:
        self._lifecycle_generation += 1
        self._retire_turn_for_reset()
        self._purge_stale_work()
        self._red_eye.reset()
        self._reset_layers()

    def _purge_stale_work(self) -> None:
        self._actions.clear()
        self._latest_frame = None
        self._wake.set()

    def _reset_layers(self) -> None:
        self._parameter_control_available = False
        self._lip_sync_available = False
        self._body_motion_available = False
        self._automatic_red_eye_available = False
        self._parameter_error_code = None
        self._lip_sync_error_code = None
        self._body_motion_error_code = None
        self._red_eye_error_code = None
        self._capabilities = {}
        self._motion_hotkey_ids = {}
        self._release_hotkey_id = None
        self._red_eye_hotkey_id = None
        self._controller = None

    def _disable_parameter_layer(self, code: str) -> None:
        self._parameter_control_available = False
        self._lip_sync_available = False
        self._parameter_error_code = code
        self._lip_sync_error_code = "avatar_parameter_unavailable"
        self._controller = None
        self._latest_frame = None
        self._notify()

    def _disable_body_layer(self, code: str) -> None:
        self._body_motion_available = False
        self._body_motion_error_code = code
        self._release_hotkey_id = None
        self._motion_hotkey_ids = {}
        self._body_pose_active = False
        self._body_replay_required = True
        self._notify()

    def _retry_delay(self, attempt: int) -> float:
        base = min(
            self._reconnect_max,
            self._reconnect_initial * (2 ** min(attempt, 16)),
        )
        jitter = 0.75 + min(1.0, max(0.0, self._rng.random())) * 0.5
        return float(min(self._reconnect_max, base * jitter))

    async def _wait_for_stop(self, delay: float) -> bool:
        sleep_task: asyncio.Future[None] = asyncio.ensure_future(self._sleep(delay))
        stop_task: asyncio.Task[None] = asyncio.create_task(
            _wait_event(self._stop),
            name="avatar-reconnect-stop",
        )
        try:
            done, pending = await asyncio.wait(
                {sleep_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return stop_task in done
        finally:
            for task in (sleep_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep_task, stop_task, return_exceptions=True)

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Avatar monotonic clock is invalid")
        return float(value)

    def _set_state(
        self,
        state: AvatarRuntimeState,
        error_code: str | None = None,
    ) -> None:
        self._state = state
        self._error_code = error_code
        self._notify()

    def _notify(self) -> None:
        listener = self._state_listener
        if listener is None:
            return
        with suppress(Exception):
            listener(self.snapshot())


def _valid_turn_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 128
        and "\x00" not in value
        and all(character.isalnum() or character in "_.:-" for character in value)
    )


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(error, SecretStoreError):
        return "vts_secret_error"
    if isinstance(error, ValueError):
        return "avatar_config_invalid"
    return "vts_unavailable"


async def _safe_close(client: AvatarClient) -> None:
    with suppress(Exception):
        await client.close()


async def _wait_event(event: asyncio.Event) -> None:
    await event.wait()
