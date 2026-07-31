"""Single-writer W28 Avatar Runtime lifecycle and backpressure tests."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest
from app.avatar import (
    AvatarParameterFrame,
    AvatarRuntime,
    AvatarRuntimeState,
    AvatarTurnPlan,
    FocusedVariant,
    RedEyeOwner,
    map_avatar_turn_plan,
)
from app.avatar import runtime as runtime_module
from app.clients.vts import (
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
from app.health import CapabilityState
from app.media import MouthEnvelopeSample


class _TokenStore:
    def __init__(self) -> None:
        self.token: VTSToken | None = VTSToken(
            "Companion",
            "Local User",
            "synthetic-token",
        )
        self.delete_count = 0
        self.save_count = 0

    async def load(self) -> VTSToken | None:
        return self.token

    async def save(self, token: VTSToken) -> None:
        self.save_count += 1
        self.token = token

    async def delete(self) -> None:
        self.delete_count += 1
        self.token = None


class _FakeClient:
    def __init__(self, *, inject_delay: float = 0.0) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.events: asyncio.Queue[VTSEvent | BaseException] = asyncio.Queue()
        self.closed_event = asyncio.Event()
        self.expression_active = False
        self.expression_state_error: BaseException | None = None
        self.inject_delay = inject_delay
        self.concurrent_injections = 0
        self.maximum_concurrent_injections = 0
        self.dropped_event_count = 0
        self.hotkeys = {
            "release_action": "release-id",
            "happy_a": "happy-a-id",
            "happy_b": "happy-b-id",
            "focused_action": "focused-id",
            "focused_chuunibyou_action": "focused-chuunibyou-id",
            "red_eye_toggle": "red-eye-id",
        }

    async def connect(self) -> None:
        self.calls.append(("connect", None))

    async def api_state(self) -> dict[str, Any]:
        return {"active": True, "currentSessionAuthenticated": False}

    async def request_token(self, _plugin_name: str, _plugin_developer: str) -> str:
        return "synthetic-token"

    async def authenticate(
        self,
        _plugin_name: str,
        _plugin_developer: str,
        _authentication_token: str,
    ) -> bool:
        self.calls.append(("authenticate", None))
        return True

    async def current_model_name(self) -> str:
        return "synthetic_model"

    async def parameter_capabilities(self) -> dict[str, VTSParameterCapability]:
        return {
            "MouthOpen": VTSParameterCapability(0.0, 1.0, 0.0),
            "MouthSmile": VTSParameterCapability(-1.0, 1.0, 0.0),
            "Brows": VTSParameterCapability(-1.0, 1.0, 0.0),
            "EyeOpenLeft": VTSParameterCapability(0.0, 1.0, 1.0),
            "EyeOpenRight": VTSParameterCapability(0.0, 1.0, 1.0),
            "EyeLeftX": VTSParameterCapability(-1.0, 1.0, 0.0),
            "EyeLeftY": VTSParameterCapability(-1.0, 1.0, 0.0),
            "EyeRightX": VTSParameterCapability(-1.0, 1.0, 0.0),
            "EyeRightY": VTSParameterCapability(-1.0, 1.0, 0.0),
            "FaceAngleX": VTSParameterCapability(-30.0, 30.0, 0.0),
            "FaceAngleY": VTSParameterCapability(-30.0, 30.0, 0.0),
            "FaceAngleZ": VTSParameterCapability(-30.0, 30.0, 0.0),
            "FacePositionY": VTSParameterCapability(-1.0, 1.0, 0.0),
        }

    async def resolve_hotkey_name(self, name: str) -> str:
        return self.hotkeys[name]

    async def subscribe_event(self, event_name: str, *, subscribe: bool = True) -> None:
        self.calls.append(("subscribe", (event_name, subscribe)))

    async def inject_parameter_values(
        self,
        values: Mapping[str, float],
        *,
        face_found: bool,
    ) -> None:
        self.concurrent_injections += 1
        self.maximum_concurrent_injections = max(
            self.maximum_concurrent_injections,
            self.concurrent_injections,
        )
        try:
            self.calls.append(("inject", (dict(values), face_found)))
            if self.inject_delay:
                await asyncio.sleep(self.inject_delay)
        finally:
            self.concurrent_injections -= 1

    async def expression_is_active(self, _expression_file: str) -> bool:
        self.calls.append(("expression_state", None))
        if self.expression_state_error is not None:
            raise self.expression_state_error
        return self.expression_active

    async def set_expression_active(
        self,
        _expression_file: str,
        *,
        active: bool,
        fade_seconds: float,
    ) -> None:
        self.expression_active = active
        self.calls.append(("expression", (active, fade_seconds)))

    async def trigger_hotkey(self, hotkey_id: str) -> None:
        self.calls.append(("hotkey", hotkey_id))

    async def next_event(self) -> VTSEvent:
        event = await self.events.get()
        if isinstance(event, BaseException):
            raise event
        return event

    async def wait_closed(self) -> None:
        await self.closed_event.wait()

    async def close(self) -> None:
        self.closed_event.set()
        self.calls.append(("close", None))


def _config(**overrides: object) -> AvatarConfig:
    values: dict[str, object] = {
        "tick_hz": 25.0,
        "release_hotkey_name": "release_action",
        "red_eye_hotkey_name": "red_eye_toggle",
        "red_eye_expression_file": "red_eye.exp3.json",
        "body_motion_hotkeys": {
            "happy": ("happy_a", "happy_b"),
            "focused": ("focused_action",),
            "focused_chuunibyou": ("focused_chuunibyou_action",),
        },
    }
    values.update(overrides)
    return AvatarConfig.model_validate(values)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


def _hotkeys(client: _FakeClient) -> list[str]:
    return [value for kind, value in client.calls if kind == "hotkey"]


def _red_eye_owner(runtime: AvatarRuntime) -> RedEyeOwner:
    return runtime.red_eye_owner


def test_body_motion_lifecycle_is_once_release_neutral_and_cancel_replay() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
            rng=random.Random(5),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        client.calls.clear()

        generation = runtime.begin_turn("turn_a")
        assert generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan("turn_a", EmotionLabel.happy),
            generation=generation,
        )
        assert runtime.arm_playback("turn_a", generation=generation)
        assert runtime.offer_mouth_envelope(
            MouthEnvelopeSample(
                turn_id="turn_a",
                playback_job_id="play_a",
                sequence=1,
                value=0.6,
            )
        )
        await _wait_until(lambda: any(value.startswith("happy-") for value in _hotkeys(client)))
        first_hotkey = next(value for value in _hotkeys(client) if value.startswith("happy-"))
        assert runtime.offer_mouth_envelope(
            MouthEnvelopeSample(
                turn_id="turn_a",
                playback_job_id="play_a",
                sequence=2,
                value=0.2,
                terminal=True,
            )
        )
        runtime.complete_turn("turn_a", generation=generation)

        same_generation = runtime.begin_turn("turn_b")
        assert same_generation is not None
        runtime.set_turn_plan(
            map_avatar_turn_plan("turn_b", EmotionLabel.happy),
            generation=same_generation,
        )
        runtime.visual_fallback("turn_b", generation=same_generation)
        await asyncio.sleep(0.03)
        assert _hotkeys(client) == [first_hotkey]
        runtime.complete_turn("turn_b", generation=same_generation)

        changed_generation = runtime.begin_turn("turn_c")
        assert changed_generation is not None
        runtime.set_turn_plan(
            map_avatar_turn_plan("turn_c", EmotionLabel.focused),
            generation=changed_generation,
        )
        runtime.visual_fallback("turn_c", generation=changed_generation)
        await _wait_until(lambda: _hotkeys(client)[-2:] == ["release-id", "focused-id"])
        runtime.complete_turn("turn_c", generation=changed_generation)

        neutral_generation = runtime.begin_turn("turn_d")
        assert neutral_generation is not None
        runtime.set_turn_plan(
            map_avatar_turn_plan("turn_d", EmotionLabel.neutral),
            generation=neutral_generation,
        )
        runtime.visual_fallback("turn_d", generation=neutral_generation)
        await _wait_until(lambda: _hotkeys(client)[-1:] == ["release-id"])
        neutral_count = len(_hotkeys(client))
        runtime.complete_turn("turn_d", generation=neutral_generation)

        replay_generation = runtime.begin_turn("turn_e")
        assert replay_generation is not None
        runtime.set_turn_plan(
            map_avatar_turn_plan("turn_e", EmotionLabel.focused),
            generation=replay_generation,
        )
        runtime.visual_fallback("turn_e", generation=replay_generation)
        await _wait_until(lambda: len(_hotkeys(client)) == neutral_count + 1)
        assert _hotkeys(client)[-1] == "focused-id"
        assert runtime.cancel_turn("turn_e", generation=replay_generation)
        await _wait_until(lambda: _hotkeys(client)[-1:] == ["release-id"])

        after_cancel = runtime.begin_turn("turn_f")
        assert after_cancel is not None
        runtime.set_turn_plan(
            map_avatar_turn_plan("turn_f", EmotionLabel.focused),
            generation=after_cancel,
        )
        runtime.visual_fallback("turn_f", generation=after_cancel)
        await _wait_until(lambda: _hotkeys(client)[-1:] == ["focused-id"])

        await runtime.close()
        assert runtime.snapshot().state is AvatarRuntimeState.stopped

    asyncio.run(scenario())


def test_focused_variant_switch_releases_and_same_variant_does_not_restart() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        client.calls.clear()

        default_generation = runtime.begin_turn("turn_focus_default")
        assert default_generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan("turn_focus_default", EmotionLabel.focused),
            generation=default_generation,
        )
        assert runtime.visual_fallback(
            "turn_focus_default",
            generation=default_generation,
        )
        await _wait_until(lambda: _hotkeys(client) == ["focused-id"])
        assert runtime.complete_turn(
            "turn_focus_default",
            generation=default_generation,
        )

        variant_generation = runtime.begin_turn("turn_focus_variant")
        assert variant_generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan(
                "turn_focus_variant",
                EmotionLabel.focused,
                focused_variant=FocusedVariant.chuunibyou,
            ),
            generation=variant_generation,
        )
        assert runtime.visual_fallback(
            "turn_focus_variant",
            generation=variant_generation,
        )
        await _wait_until(lambda: _hotkeys(client)[-2:] == ["release-id", "focused-chuunibyou-id"])
        variant_count = len(_hotkeys(client))
        assert runtime.complete_turn(
            "turn_focus_variant",
            generation=variant_generation,
        )

        same_generation = runtime.begin_turn("turn_focus_same")
        assert same_generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan(
                "turn_focus_same",
                EmotionLabel.focused,
                focused_variant=FocusedVariant.chuunibyou,
            ),
            generation=same_generation,
        )
        assert runtime.visual_fallback("turn_focus_same", generation=same_generation)
        await asyncio.sleep(0.03)
        assert len(_hotkeys(client)) == variant_count
        assert runtime.complete_turn("turn_focus_same", generation=same_generation)

        back_generation = runtime.begin_turn("turn_focus_back")
        assert back_generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan("turn_focus_back", EmotionLabel.focused),
            generation=back_generation,
        )
        assert runtime.visual_fallback("turn_focus_back", generation=back_generation)
        await _wait_until(lambda: _hotkeys(client)[-2:] == ["release-id", "focused-id"])

        await runtime.close()

    asyncio.run(scenario())


def test_red_eye_system_manual_ownership_and_model_reset() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(red_eye_duration_seconds=0.04),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        client.calls.clear()

        assert runtime.trigger_red_eye()
        await _wait_until(lambda: ("expression", (True, 0.1)) in client.calls)
        assert _red_eye_owner(runtime) is RedEyeOwner.system
        assert runtime.trigger_red_eye()
        assert [call for call in client.calls if call == ("expression", (True, 0.1))] == [
            ("expression", (True, 0.1))
        ]
        await _wait_until(
            lambda: ("expression", (False, 0.1)) in client.calls,
            timeout=0.5,
        )
        assert _red_eye_owner(runtime) is RedEyeOwner.off

        client.calls.clear()
        client.expression_active = True
        await client.events.put(
            VTSHotkeyTriggeredEvent(
                hotkey_name=" RED_EYE_TOGGLE ",
                triggered_by_api=False,
            )
        )
        await _wait_until(lambda: runtime.red_eye_owner is RedEyeOwner.manual)
        assert runtime.trigger_red_eye()
        await asyncio.sleep(0.07)
        assert ("expression", (False, 0.1)) not in client.calls

        client.expression_active = False
        await client.events.put(
            VTSHotkeyTriggeredEvent(
                hotkey_name="red_eye_toggle",
                triggered_by_api=False,
            )
        )
        await _wait_until(lambda: runtime.red_eye_owner is RedEyeOwner.off)

        client.calls.clear()
        await client.events.put(VTSModelLoadedEvent(model_loaded=True))
        await _wait_until(
            lambda: (
                ("expression", (False, 0.1)) in client.calls and "release-id" in _hotkeys(client)
            )
        )
        assert runtime.red_eye_owner is RedEyeOwner.off
        await runtime.close()

    asyncio.run(scenario())


def test_slow_vts_has_one_inflight_injection_and_latest_frame_coalescing() -> None:
    async def scenario() -> None:
        client = _FakeClient(inject_delay=0.025)
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(tick_hz=60.0),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        await asyncio.sleep(0.16)

        snapshot = runtime.snapshot()
        assert client.maximum_concurrent_injections == 1
        assert snapshot.sent_frames > 0
        assert snapshot.coalesced_frames > 0
        assert "synthetic_model" not in repr(snapshot)
        assert "release-id" not in repr(snapshot)
        await runtime.close()

    asyncio.run(scenario())


def test_turn_accepted_before_first_connection_is_not_invalidated() -> None:
    async def scenario() -> None:
        release_connect = asyncio.Event()

        class DeferredClient(_FakeClient):
            async def connect(self) -> None:
                await release_connect.wait()
                await super().connect()

        client = DeferredClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        runtime.start()
        generation = runtime.begin_turn("turn_before_ready")
        assert generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan("turn_before_ready", EmotionLabel.happy),
            generation=generation,
        )
        assert runtime.visual_fallback(
            "turn_before_ready",
            generation=generation,
        )

        release_connect.set()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        await _wait_until(lambda: any(value.startswith("happy-") for value in _hotkeys(client)))
        await runtime.close()

    asyncio.run(scenario())


def test_layer_failures_degrade_independently_without_disabling_runtime() -> None:
    async def scenario() -> None:
        class ParameterFailureClient(_FakeClient):
            async def parameter_capabilities(
                self,
            ) -> dict[str, VTSParameterCapability]:
                raise VTSAPIError(400)

        parameter_client = ParameterFailureClient()
        parameter_runtime = AvatarRuntime(
            lambda: parameter_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        parameter_runtime.start()
        await _wait_until(lambda: parameter_runtime.snapshot().state is AvatarRuntimeState.ready)
        parameter_snapshot = parameter_runtime.snapshot()
        assert not parameter_snapshot.parameter_control_available
        assert not parameter_snapshot.lip_sync_available
        assert parameter_snapshot.body_motion_available
        assert parameter_snapshot.automatic_red_eye_available
        assert parameter_snapshot.parameter_error_code == "avatar_parameter_unavailable"
        await parameter_runtime.close()

        class MissingMouthClient(_FakeClient):
            async def parameter_capabilities(
                self,
            ) -> dict[str, VTSParameterCapability]:
                capabilities = await super().parameter_capabilities()
                capabilities.pop("MouthOpen")
                return capabilities

        mouth_client = MissingMouthClient()
        mouth_runtime = AvatarRuntime(
            lambda: mouth_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        mouth_runtime.start()
        await _wait_until(lambda: mouth_runtime.snapshot().state is AvatarRuntimeState.ready)
        mouth_snapshot = mouth_runtime.snapshot()
        assert mouth_snapshot.parameter_control_available
        assert not mouth_snapshot.lip_sync_available
        assert mouth_snapshot.lip_sync_error_code == "avatar_mouth_parameter_missing"
        await mouth_runtime.close()

        class MissingReleaseClient(_FakeClient):
            async def resolve_hotkey_name(self, name: str) -> str:
                if name == "release_action":
                    raise KeyError(name)
                return await super().resolve_hotkey_name(name)

        release_client = MissingReleaseClient()
        release_runtime = AvatarRuntime(
            lambda: release_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        release_runtime.start()
        await _wait_until(lambda: release_runtime.snapshot().state is AvatarRuntimeState.ready)
        release_snapshot = release_runtime.snapshot()
        assert release_snapshot.parameter_control_available
        assert not release_snapshot.body_motion_available
        assert release_snapshot.automatic_red_eye_available
        assert release_snapshot.body_motion_error_code == "avatar_release_unavailable"
        await release_runtime.close()

    asyncio.run(scenario())


def test_intentionally_disabled_layers_remain_reason_visible_without_degraded_health() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(
                parameter_control_enabled=False,
                lip_sync_enabled=False,
                body_motion_enabled=False,
                auto_red_eye_enabled=False,
            ),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)

        snapshot = runtime.snapshot()
        assert not snapshot.parameter_control_available
        assert not snapshot.lip_sync_available
        assert not snapshot.body_motion_available
        assert not snapshot.automatic_red_eye_available
        assert snapshot.parameter_error_code == "avatar_parameter_disabled"
        assert snapshot.lip_sync_error_code == "avatar_lip_sync_disabled"
        assert snapshot.body_motion_error_code == "avatar_body_disabled"
        assert snapshot.red_eye_error_code == "avatar_red_eye_disabled"
        health = await runtime.check_health()
        assert health.status is CapabilityState.ready
        assert health.error_code is None
        assert not any(kind == "inject" for kind, _payload in client.calls)

        await runtime.close()

    asyncio.run(scenario())


def test_event_overflow_disables_automatic_red_eye_without_closing_manual_state() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        client.calls.clear()
        assert runtime.trigger_red_eye()
        await _wait_until(lambda: ("expression", (True, 0.1)) in client.calls)
        client.calls.clear()
        client.dropped_event_count = 1

        await _wait_until(lambda: not runtime.snapshot().automatic_red_eye_available)
        assert runtime.snapshot().red_eye_error_code == "avatar_event_overflow"
        assert runtime.red_eye_owner is RedEyeOwner.manual
        assert ("expression", (False, 0.1)) not in client.calls
        assert ("expression_state", None) in client.calls
        assert not runtime.trigger_red_eye()
        await runtime.close()

    asyncio.run(scenario())


def test_event_overflow_query_failure_preserves_possible_manual_state() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        assert runtime.trigger_red_eye()
        await _wait_until(lambda: client.expression_active)
        client.calls.clear()
        client.expression_state_error = VTSAPIError(500)
        client.dropped_event_count = 1

        await _wait_until(lambda: not runtime.snapshot().automatic_red_eye_available)
        snapshot = runtime.snapshot()
        assert snapshot.red_eye_error_code == "avatar_event_overflow"
        assert runtime.red_eye_owner is RedEyeOwner.manual
        assert ("expression_state", None) in client.calls
        assert ("expression", (False, 0.1)) not in client.calls
        assert not runtime.trigger_red_eye()
        await runtime.close()

    asyncio.run(scenario())


def test_reconnect_invalidates_old_turn_and_never_replays_its_motion() -> None:
    async def scenario() -> None:
        first = _FakeClient()
        second = _FakeClient()
        clients = iter((first, second))

        def factory() -> _FakeClient:
            return next(clients, second)

        runtime = AvatarRuntime(
            factory,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
            reconnect_initial_seconds=0.001,
            reconnect_max_seconds=0.002,
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)

        generation = runtime.begin_turn("turn_stale")
        assert generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan("turn_stale", EmotionLabel.happy),
            generation=generation,
        )
        await first.events.put(VTSConnectionError())
        await _wait_until(
            lambda: (
                runtime.snapshot().reconnect_count >= 1
                and runtime.snapshot().state is AvatarRuntimeState.ready
            )
        )
        second.calls.clear()

        assert not runtime.visual_fallback("turn_stale", generation=generation)
        await asyncio.sleep(0.05)
        assert not any(value.startswith("happy-") for value in _hotkeys(second))
        await runtime.close()

    asyncio.run(scenario())


def test_connection_failure_cleans_and_closes_once_before_backoff() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        backoff_entered = asyncio.Event()

        async def blocked_backoff(delay: float) -> None:
            if delay < 0.5:
                await asyncio.sleep(0)
                return
            backoff_entered.set()
            await asyncio.Future()

        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
            sleep=blocked_backoff,
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        client.calls.clear()

        await client.events.put(VTSConnectionError())
        await asyncio.wait_for(backoff_entered.wait(), timeout=1.0)

        assert sum(call == ("hotkey", "release-id") for call in client.calls) == 1
        assert sum(call == ("expression", (False, 0.1)) for call in client.calls) == 1
        assert sum(kind == "close" for kind, _payload in client.calls) == 1
        assert (
            sum(
                kind == "inject" and payload[0].get("MouthOpen") == 0.0
                for kind, payload in client.calls
            )
            == 1
        )

        await runtime.close()
        assert sum(call == ("hotkey", "release-id") for call in client.calls) == 1
        assert sum(call == ("expression", (False, 0.1)) for call in client.calls) == 1
        assert sum(kind == "close" for kind, _payload in client.calls) == 1

    asyncio.run(scenario())


def test_slow_vts_coalesces_ten_thousand_frames_and_close_leaks_no_tasks() -> None:
    async def scenario() -> None:
        class FastClock:
            def __init__(self) -> None:
                self.now = 0.0

            def __call__(self) -> float:
                return self.now

            async def sleep(self, delay: float) -> None:
                self.now += delay
                await asyncio.sleep(0)

        class BlockedInjectionClient(_FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.block_injections = False
                self.injection_started = asyncio.Event()
                self.injection_gate = asyncio.Event()

            async def inject_parameter_values(
                self,
                values: Mapping[str, float],
                *,
                face_found: bool,
            ) -> None:
                if self.block_injections:
                    self.injection_started.set()
                    await self.injection_gate.wait()
                await super().inject_parameter_values(values, face_found=face_found)

        clock = FastClock()
        client = BlockedInjectionClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
            clock=clock,
            sleep=clock.sleep,
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)

        client.block_injections = True
        await asyncio.wait_for(client.injection_started.wait(), timeout=1.0)
        await _wait_until(
            lambda: runtime.snapshot().coalesced_frames >= 10_000,
            timeout=3.0,
        )
        live_avatar_tasks = {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done() and task.get_name().startswith("avatar-")
        }
        assert live_avatar_tasks <= {
            "avatar-runtime",
            "avatar-vts-events",
            "avatar-frame-producer",
            "avatar-vts-disconnected",
            "avatar-runtime-stopped",
            "avatar-runtime-wake",
        }

        client.injection_gate.set()
        await runtime.close()
        await asyncio.sleep(0)

        snapshot = runtime.snapshot()
        assert snapshot.coalesced_frames >= 10_000
        assert snapshot.state is AvatarRuntimeState.stopped
        assert not {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done() and task.get_name().startswith("avatar-")
        }

    asyncio.run(scenario())


def test_close_is_idempotent_and_cleans_mouth_body_and_red_eye_before_socket() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(red_eye_duration_seconds=1.0),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        client.calls.clear()

        generation = runtime.begin_turn("turn_close")
        assert generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan("turn_close", EmotionLabel.happy),
            generation=generation,
        )
        assert runtime.visual_fallback("turn_close", generation=generation)
        assert runtime.trigger_red_eye()
        await _wait_until(
            lambda: (
                any(value.startswith("happy-") for value in _hotkeys(client))
                and ("expression", (True, 0.1)) in client.calls
            )
        )

        await runtime.close()
        await runtime.close()

        close_index = max(index for index, call in enumerate(client.calls) if call[0] == "close")
        assert any(
            kind == "inject" and payload[0].get("MouthOpen") == 0.0
            for kind, payload in client.calls[:close_index]
        )
        assert ("hotkey", "release-id") in client.calls[:close_index]
        assert ("expression", (False, 0.1)) in client.calls[:close_index]
        assert runtime.red_eye_owner is RedEyeOwner.off
        assert runtime.snapshot().state is AvatarRuntimeState.stopped

    asyncio.run(scenario())


def test_runtime_validates_configuration_and_public_lifecycle_contract() -> None:
    with pytest.raises(ValueError, match="identity"):
        AvatarRuntime(
            _FakeClient,
            _TokenStore(),
            plugin_name=" ",
            plugin_developer="Local User",
            config=_config(),
        )
    with pytest.raises(ValueError, match="reconnect"):
        AvatarRuntime(
            _FakeClient,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
            reconnect_initial_seconds=0.0,
        )
    with pytest.raises(ValueError, match="reconnect"):
        AvatarRuntime(
            _FakeClient,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
            reconnect_initial_seconds=2.0,
            reconnect_max_seconds=1.0,
        )

    async def scenario() -> None:
        def broken_listener(_snapshot: object) -> None:
            raise RuntimeError("synthetic listener failure")

        disabled = AvatarRuntime(
            _FakeClient,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(enabled=False),
            state_listener=broken_listener,
        )
        assert (await disabled.check_health()).status is CapabilityState.disabled
        disabled.start()
        assert disabled.snapshot().state is AvatarRuntimeState.disabled
        disabled_health = await disabled.check_health()
        assert disabled_health.status is CapabilityState.disabled
        assert disabled_health.error_code == "avatar_disabled"
        assert disabled.begin_turn("turn_disabled") is None
        assert not disabled.trigger_red_eye()
        await disabled.close()
        with pytest.raises(RuntimeError, match="closed"):
            disabled.start()

        connect_entered = asyncio.Event()
        release_connect = asyncio.Event()

        class DeferredClient(_FakeClient):
            async def connect(self) -> None:
                connect_entered.set()
                await release_connect.wait()
                await super().connect()

        client = DeferredClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        runtime.start()
        runtime.start()
        await asyncio.wait_for(connect_entered.wait(), timeout=1.0)
        connecting_health = await runtime.check_health()
        assert connecting_health.status is CapabilityState.unavailable
        assert connecting_health.error_code == "avatar_not_ready"

        for invalid_turn_id in ("", " padded", "bad value", "bad\x00id", "x" * 129):
            assert runtime.begin_turn(invalid_turn_id) is None
        generation = runtime.begin_turn("turn_contract")
        assert generation is not None
        plan = map_avatar_turn_plan("turn_contract", EmotionLabel.happy)
        assert not runtime.set_turn_plan(plan, generation=generation + 1)
        assert not runtime.set_turn_plan(
            cast(AvatarTurnPlan, object()),
            generation=generation,
        )
        assert runtime.set_turn_plan(plan, generation=generation)
        assert not runtime.set_turn_plan(plan, generation=generation)
        assert not runtime.arm_playback("turn_contract", generation=generation + 1)
        assert runtime.arm_playback("turn_contract", generation=generation)
        assert not runtime.offer_mouth_envelope(cast(MouthEnvelopeSample, object()))
        assert not runtime.offer_mouth_envelope(
            MouthEnvelopeSample(
                turn_id="another_turn",
                playback_job_id="play_contract",
                sequence=1,
                value=0.2,
            )
        )
        assert runtime.offer_mouth_envelope(
            MouthEnvelopeSample(
                turn_id="turn_contract",
                playback_job_id="play_contract",
                sequence=2,
                value=0.4,
            )
        )
        assert not runtime.offer_mouth_envelope(
            MouthEnvelopeSample(
                turn_id="turn_contract",
                playback_job_id="play_contract",
                sequence=2,
                value=0.5,
            )
        )
        assert not runtime.offer_mouth_envelope(
            MouthEnvelopeSample(
                turn_id="turn_contract",
                playback_job_id="another_playback",
                sequence=3,
                value=0.5,
            )
        )
        assert runtime.visual_fallback("turn_contract", generation=generation)
        assert not runtime.visual_fallback("turn_contract", generation=generation + 1)
        assert not runtime.complete_turn("turn_contract", generation=generation + 1)
        assert not runtime.cancel_turn("turn_contract", generation=generation + 1)

        release_connect.set()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        assert runtime.complete_turn("turn_contract", generation=generation)
        await runtime.close()

    asyncio.run(scenario())


def test_authorization_rejects_malformed_states_and_rotates_invalid_tokens() -> None:
    async def scenario() -> None:
        class MalformedStateClient(_FakeClient):
            async def api_state(self) -> dict[str, Any]:
                return {"active": "yes", "currentSessionAuthenticated": False}

        runtime = AvatarRuntime(
            _FakeClient,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        with pytest.raises(VTSProtocolError):
            await runtime._authorize(MalformedStateClient())

        class InactiveClient(_FakeClient):
            async def api_state(self) -> dict[str, Any]:
                return {"active": False, "currentSessionAuthenticated": False}

        with pytest.raises(VTSConfigurationError, match="vts_api_unavailable"):
            await runtime._authorize(InactiveClient())

        rotated_store = _TokenStore()
        rotated_store.token = VTSToken("Other Plugin", "Other Developer", "stale-token")
        rotated_runtime = AvatarRuntime(
            _FakeClient,
            rotated_store,
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        await rotated_runtime._authorize(_FakeClient())
        assert rotated_store.delete_count == 1
        assert rotated_store.save_count == 1
        assert rotated_store.token == VTSToken("Companion", "Local User", "synthetic-token")

        class StoredTimeoutClient(_FakeClient):
            async def authenticate(
                self,
                _plugin_name: str,
                _plugin_developer: str,
                _authentication_token: str,
            ) -> bool:
                raise VTSRequestTimeout

        with pytest.raises(VTSAuthenticationError):
            await runtime._authorize(StoredTimeoutClient())

        rejected_store = _TokenStore()

        class RejectedStoredClient(_FakeClient):
            async def authenticate(
                self,
                _plugin_name: str,
                _plugin_developer: str,
                _authentication_token: str,
            ) -> bool:
                return False

            async def request_token(self, _plugin_name: str, _plugin_developer: str) -> str:
                raise VTSAPIError(403)

        with pytest.raises(VTSAuthenticationError):
            await AvatarRuntime(
                _FakeClient,
                rejected_store,
                plugin_name="Companion",
                plugin_developer="Local User",
                config=_config(),
            )._authorize(RejectedStoredClient())
        assert rejected_store.delete_count == 1

        empty_store = _TokenStore()
        empty_store.token = None

        class RejectedNewClient(_FakeClient):
            async def authenticate(
                self,
                _plugin_name: str,
                _plugin_developer: str,
                _authentication_token: str,
            ) -> bool:
                return False

        with pytest.raises(VTSAuthenticationError):
            await AvatarRuntime(
                _FakeClient,
                empty_store,
                plugin_name="Companion",
                plugin_developer="Local User",
                config=_config(),
            )._authorize(RejectedNewClient())

    asyncio.run(scenario())


def test_runtime_degrades_unconfigured_partial_and_unmapped_discrete_layers() -> None:
    async def scenario() -> None:
        unconfigured_client = _FakeClient()
        unconfigured = AvatarRuntime(
            lambda: unconfigured_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(
                release_hotkey_name="",
                red_eye_hotkey_name="",
                red_eye_expression_file="",
            ),
        )
        unconfigured.start()
        await _wait_until(lambda: unconfigured.snapshot().state is AvatarRuntimeState.ready)
        unconfigured_snapshot = unconfigured.snapshot()
        assert unconfigured_snapshot.body_motion_error_code == "avatar_release_unconfigured"
        assert unconfigured_snapshot.red_eye_error_code == "avatar_red_eye_unconfigured"
        await unconfigured.close()

        class PartialMappingClient(_FakeClient):
            async def resolve_hotkey_name(self, name: str) -> str:
                if name == "happy_a":
                    raise KeyError(name)
                return await super().resolve_hotkey_name(name)

        client = PartialMappingClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        runtime.start()
        await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)
        assert runtime.snapshot().body_motion_available
        assert runtime.snapshot().body_motion_error_code == "avatar_motion_mapping_partial"
        client.calls.clear()

        focused_generation = runtime.begin_turn("turn_focused")
        assert focused_generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan("turn_focused", EmotionLabel.focused),
            generation=focused_generation,
        )
        assert runtime.visual_fallback("turn_focused", generation=focused_generation)
        await _wait_until(lambda: "focused-id" in _hotkeys(client))
        assert runtime.complete_turn("turn_focused", generation=focused_generation)

        happy_generation = runtime.begin_turn("turn_partial")
        assert happy_generation is not None
        assert runtime.set_turn_plan(
            map_avatar_turn_plan("turn_partial", EmotionLabel.happy),
            generation=happy_generation,
        )
        assert runtime.visual_fallback("turn_partial", generation=happy_generation)
        await _wait_until(lambda: "happy-b-id" in _hotkeys(client))
        assert "happy-a-id" not in _hotkeys(client)
        assert runtime.complete_turn("turn_partial", generation=happy_generation)
        await runtime.close()

        unmapped_client = _FakeClient()
        unmapped = AvatarRuntime(
            lambda: unmapped_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(
                body_motion_hotkeys={
                    "focused": ("focused_action",),
                }
            ),
        )
        unmapped.start()
        await _wait_until(lambda: unmapped.snapshot().state is AvatarRuntimeState.ready)

        mapped_generation = unmapped.begin_turn("turn_mapped")
        assert mapped_generation is not None
        assert unmapped.set_turn_plan(
            map_avatar_turn_plan("turn_mapped", EmotionLabel.focused),
            generation=mapped_generation,
        )
        assert unmapped.visual_fallback("turn_mapped", generation=mapped_generation)
        await _wait_until(lambda: "focused-id" in _hotkeys(unmapped_client))
        assert unmapped.complete_turn("turn_mapped", generation=mapped_generation)
        unmapped_client.calls.clear()

        unmapped_generation = unmapped.begin_turn("turn_unmapped")
        assert unmapped_generation is not None
        assert unmapped.set_turn_plan(
            map_avatar_turn_plan("turn_unmapped", EmotionLabel.happy),
            generation=unmapped_generation,
        )
        assert unmapped.visual_fallback("turn_unmapped", generation=unmapped_generation)
        await _wait_until(
            lambda: unmapped.snapshot().body_motion_error_code == "avatar_motion_unmapped"
        )
        await _wait_until(lambda: _hotkeys(unmapped_client)[-1:] == ["release-id"])

        await unmapped_client.events.put(VTSModelLoadedEvent(model_loaded=False))
        await _wait_until(
            lambda: (
                unmapped.snapshot().state is AvatarRuntimeState.preparing
                and unmapped.snapshot().error_code == "vts_model_missing"
            )
        )
        assert not unmapped.snapshot().parameter_control_available
        assert not unmapped.snapshot().body_motion_available
        await unmapped.close()

    asyncio.run(scenario())


def test_runtime_write_failures_degrade_only_the_affected_layer() -> None:
    async def scenario() -> None:
        class ParameterWriteFailureClient(_FakeClient):
            fail_parameter_frame = False

            async def inject_parameter_values(
                self,
                values: Mapping[str, float],
                *,
                face_found: bool,
            ) -> None:
                if self.fail_parameter_frame and len(values) > 1:
                    raise VTSAPIError(500)
                await super().inject_parameter_values(values, face_found=face_found)

        parameter_client = ParameterWriteFailureClient()
        parameter_runtime = AvatarRuntime(
            lambda: parameter_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        parameter_runtime.start()
        await _wait_until(lambda: parameter_runtime.snapshot().state is AvatarRuntimeState.ready)
        parameter_client.fail_parameter_frame = True
        await _wait_until(lambda: not parameter_runtime.snapshot().parameter_control_available)
        parameter_snapshot = parameter_runtime.snapshot()
        assert parameter_snapshot.parameter_error_code == "avatar_parameter_write_failed"
        assert parameter_snapshot.lip_sync_error_code == "avatar_parameter_unavailable"
        parameter_health = await parameter_runtime.check_health()
        assert parameter_health.status is CapabilityState.degraded
        assert parameter_health.error_code == "avatar_parameter_write_failed"
        await parameter_runtime.close()

        class MouthZeroFailureClient(_FakeClient):
            fail_mouth_zero = False

            async def inject_parameter_values(
                self,
                values: Mapping[str, float],
                *,
                face_found: bool,
            ) -> None:
                if self.fail_mouth_zero and set(values) == {"MouthOpen"}:
                    raise VTSAPIError(500)
                await super().inject_parameter_values(values, face_found=face_found)

        mouth_client = MouthZeroFailureClient()
        mouth_runtime = AvatarRuntime(
            lambda: mouth_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        mouth_runtime.start()
        await _wait_until(lambda: mouth_runtime.snapshot().state is AvatarRuntimeState.ready)
        mouth_generation = mouth_runtime.begin_turn("turn_mouth_failure")
        assert mouth_generation is not None
        assert mouth_runtime.set_turn_plan(
            map_avatar_turn_plan("turn_mouth_failure", EmotionLabel.happy),
            generation=mouth_generation,
        )
        mouth_client.fail_mouth_zero = True
        assert mouth_runtime.cancel_turn("turn_mouth_failure", generation=mouth_generation)
        await _wait_until(lambda: not mouth_runtime.snapshot().lip_sync_available)
        assert mouth_runtime.snapshot().lip_sync_error_code == "avatar_mouth_write_failed"
        assert mouth_runtime.snapshot().parameter_control_available
        await mouth_runtime.close()

        class HotkeyFailureClient(_FakeClient):
            fail_hotkey_id: str | None = None

            async def trigger_hotkey(self, hotkey_id: str) -> None:
                if hotkey_id == self.fail_hotkey_id:
                    raise VTSAPIError(500)
                await super().trigger_hotkey(hotkey_id)

        motion_client = HotkeyFailureClient()
        motion_runtime = AvatarRuntime(
            lambda: motion_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(body_motion_hotkeys={"happy": ("happy_a",)}),
        )
        motion_runtime.start()
        await _wait_until(lambda: motion_runtime.snapshot().state is AvatarRuntimeState.ready)
        motion_client.fail_hotkey_id = "happy-a-id"
        motion_generation = motion_runtime.begin_turn("turn_motion_failure")
        assert motion_generation is not None
        assert motion_runtime.set_turn_plan(
            map_avatar_turn_plan("turn_motion_failure", EmotionLabel.happy),
            generation=motion_generation,
        )
        assert motion_runtime.visual_fallback(
            "turn_motion_failure",
            generation=motion_generation,
        )
        await _wait_until(lambda: not motion_runtime.snapshot().body_motion_available)
        assert motion_runtime.snapshot().body_motion_error_code == "avatar_motion_failed"
        await motion_runtime.close()

        release_client = HotkeyFailureClient()
        release_runtime = AvatarRuntime(
            lambda: release_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(
                body_motion_hotkeys={"focused": ("focused_action",), "happy": ("happy_a",)}
            ),
        )
        release_runtime.start()
        await _wait_until(lambda: release_runtime.snapshot().state is AvatarRuntimeState.ready)
        focused_generation = release_runtime.begin_turn("turn_before_release_failure")
        assert focused_generation is not None
        assert release_runtime.set_turn_plan(
            map_avatar_turn_plan("turn_before_release_failure", EmotionLabel.focused),
            generation=focused_generation,
        )
        assert release_runtime.visual_fallback(
            "turn_before_release_failure",
            generation=focused_generation,
        )
        await _wait_until(lambda: "focused-id" in _hotkeys(release_client))
        assert release_runtime.complete_turn(
            "turn_before_release_failure",
            generation=focused_generation,
        )
        release_client.fail_hotkey_id = "release-id"
        happy_generation = release_runtime.begin_turn("turn_release_failure")
        assert happy_generation is not None
        assert release_runtime.set_turn_plan(
            map_avatar_turn_plan("turn_release_failure", EmotionLabel.happy),
            generation=happy_generation,
        )
        assert release_runtime.visual_fallback(
            "turn_release_failure",
            generation=happy_generation,
        )
        await _wait_until(lambda: not release_runtime.snapshot().body_motion_available)
        assert release_runtime.snapshot().body_motion_error_code == "avatar_release_failed"
        await release_runtime.close()

        class RedEyeWriteFailureClient(_FakeClient):
            async def set_expression_active(
                self,
                _expression_file: str,
                *,
                active: bool,
                fade_seconds: float,
            ) -> None:
                if active:
                    raise VTSAPIError(500)
                await super().set_expression_active(
                    _expression_file,
                    active=active,
                    fade_seconds=fade_seconds,
                )

        red_eye_client = RedEyeWriteFailureClient()
        red_eye_runtime = AvatarRuntime(
            lambda: red_eye_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        red_eye_runtime.start()
        await _wait_until(lambda: red_eye_runtime.snapshot().state is AvatarRuntimeState.ready)
        assert red_eye_runtime.trigger_red_eye()
        await _wait_until(lambda: not red_eye_runtime.snapshot().automatic_red_eye_available)
        assert red_eye_runtime.snapshot().red_eye_error_code == "avatar_red_eye_write_failed"
        assert red_eye_runtime.red_eye_owner is RedEyeOwner.off
        await red_eye_runtime.close()

        reconcile_client = _FakeClient()
        reconcile_runtime = AvatarRuntime(
            lambda: reconcile_client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(),
        )
        reconcile_runtime.start()
        await _wait_until(lambda: reconcile_runtime.snapshot().state is AvatarRuntimeState.ready)
        reconcile_client.expression_state_error = VTSAPIError(500)
        await reconcile_client.events.put(
            VTSHotkeyTriggeredEvent(
                hotkey_name="red_eye_toggle",
                triggered_by_api=False,
            )
        )
        await _wait_until(lambda: not reconcile_runtime.snapshot().automatic_red_eye_available)
        assert reconcile_runtime.snapshot().red_eye_error_code == "avatar_red_eye_state_failed"
        assert reconcile_runtime.red_eye_owner is RedEyeOwner.manual
        await reconcile_runtime.close()

    asyncio.run(scenario())


def test_runtime_internal_safety_gates_drop_stale_or_lower_priority_work() -> None:
    class _CodedError(RuntimeError):
        code = "synthetic_stable_code"

    assert runtime_module._error_code(_CodedError()) == "synthetic_stable_code"
    assert runtime_module._error_code(ValueError("synthetic")) == "avatar_config_invalid"
    assert runtime_module._error_code(RuntimeError("synthetic")) == "vts_unavailable"

    async def scenario() -> None:
        client = _FakeClient()
        runtime = AvatarRuntime(
            lambda: client,
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=_config(
                urgent_queue_capacity=4,
                red_eye_hotkey_name="",
                red_eye_expression_file="",
            ),
            clock=lambda: cast(float, True),
        )

        with pytest.raises(ValueError, match="clock"):
            runtime._now()
        runtime._schedule_or_defer_visual()
        runtime._schedule_body_plan(map_avatar_turn_plan("turn_safety", EmotionLabel.happy))

        stale_action = runtime_module._Action(
            kind=runtime_module._ActionKind.mouth_zero,
            priority=runtime_module._Priority.safety,
            sequence=1,
            lifecycle_generation=runtime._lifecycle_generation + 1,
            vts_generation=runtime._vts_generation,
            model_generation=runtime._model_generation,
        )
        await runtime._process_action(client, stale_action)
        await runtime._send_frame(
            client,
            AvatarParameterFrame(
                vts_generation=1,
                model_generation=1,
                sequence=1,
                parameters=(("MouthOpen", 0.0),),
            ),
        )
        await runtime._send_mouth_zero(client)
        assert not await runtime._send_release(client)
        await runtime._send_body_transition(
            client,
            runtime_module._BodyTransition(
                emotion=EmotionLabel.happy,
                motion_hotkey_id="synthetic-motion",
                release_first=False,
            ),
        )
        runtime._body_motion_available = True
        await runtime._send_body_transition(
            client,
            runtime_module._BodyTransition(
                emotion=EmotionLabel.happy,
                motion_hotkey_id=None,
                release_first=False,
            ),
        )
        await runtime._set_red_eye(client, active=True)
        await runtime._reconcile_red_eye(client)
        assert client.calls == []

        for _ in range(4):
            assert runtime._queue_action(
                runtime_module._ActionKind.body_transition,
                runtime_module._Priority.body,
            )
        assert runtime._queue_action(
            runtime_module._ActionKind.release,
            runtime_module._Priority.safety,
        )
        for _ in range(3):
            assert runtime._queue_action(
                runtime_module._ActionKind.mouth_zero,
                runtime_module._Priority.safety,
            )
        assert not runtime._queue_action(
            runtime_module._ActionKind.body_transition,
            runtime_module._Priority.body,
        )
        assert runtime.snapshot().dropped_actions == 5
        assert [action.kind for action in runtime._actions] == [
            runtime_module._ActionKind.release,
            runtime_module._ActionKind.mouth_zero,
            runtime_module._ActionKind.mouth_zero,
            runtime_module._ActionKind.mouth_zero,
        ]

        await runtime.close()
        assert not runtime._queue_action(
            runtime_module._ActionKind.release,
            runtime_module._Priority.safety,
        )

    asyncio.run(scenario())
