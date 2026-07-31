"""AvatarRuntime integration against the real VTS WebSocket client."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from typing import Any, cast

from app.avatar import (
    AvatarRuntime,
    AvatarRuntimeState,
    RedEyeOwner,
    map_avatar_turn_plan,
)
from app.clients.vts import VTSClient, VTSToken
from app.config.settings import AvatarConfig
from app.emotion import EmotionLabel
from websockets.asyncio.server import Server, ServerConnection, serve


class _TokenStore:
    def __init__(self) -> None:
        self.token: VTSToken | None = VTSToken(
            "Companion",
            "Local User",
            "synthetic-avatar-token",
        )

    async def load(self) -> VTSToken | None:
        return self.token

    async def save(self, token: VTSToken) -> None:
        self.token = token

    async def delete(self) -> None:
        self.token = None


class _AvatarVTSServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.connection: ServerConnection | None = None
        self.connection_closed = asyncio.Event()
        self.expression_active = False
        self.model_name = "synthetic-private-model"
        self.event_count = 0

    async def handler(self, connection: ServerConnection) -> None:
        self.connection = connection
        authenticated = False
        subscribed_events: set[str] = set()
        try:
            async for raw in connection:
                request = json.loads(raw)
                assert isinstance(request, dict)
                self.requests.append(request)
                message_type = request["messageType"]
                data: dict[str, Any]
                if message_type == "APIStateRequest":
                    data = {
                        "active": True,
                        "currentSessionAuthenticated": authenticated,
                    }
                elif message_type == "AuthenticationRequest":
                    authenticated = (
                        request["data"].get("authenticationToken") == "synthetic-avatar-token"
                    )
                    data = {"authenticated": authenticated}
                elif message_type == "CurrentModelRequest":
                    data = {
                        "modelLoaded": True,
                        "modelName": self.model_name,
                        "modelID": "synthetic-private-model-id",
                    }
                elif message_type == "EventSubscriptionRequest":
                    event_name = request["data"]["eventName"]
                    if request["data"]["subscribe"]:
                        subscribed_events.add(event_name)
                    else:
                        subscribed_events.discard(event_name)
                    current_events = sorted(subscribed_events)
                    data = {
                        "subscribedEventCount": len(current_events),
                        "subscribedEvents": current_events,
                    }
                elif message_type == "InputParameterListRequest":
                    data = {
                        "defaultParameters": _parameter_capabilities(),
                        "customParameters": [],
                    }
                elif message_type == "HotkeysInCurrentModelRequest":
                    data = {
                        "modelLoaded": True,
                        "availableHotkeys": [
                            {"name": "release_action", "hotkeyID": "release-id"},
                            {"name": "happy_action", "hotkeyID": "happy-id"},
                            {"name": "focused_action", "hotkeyID": "focused-id"},
                            {"name": "red_eye_toggle", "hotkeyID": "red-eye-id"},
                        ],
                    }
                elif message_type == "ExpressionStateRequest":
                    data = {
                        "expressions": [
                            {
                                "file": "red_eye.exp3.json",
                                "active": self.expression_active,
                            }
                        ]
                    }
                elif message_type == "ExpressionActivationRequest":
                    self.expression_active = request["data"]["active"]
                    data = {}
                elif message_type in {
                    "InjectParameterDataRequest",
                    "HotkeyTriggerRequest",
                }:
                    data = {}
                else:
                    raise AssertionError(f"unsupported fake VTS request: {message_type}")
                await connection.send(_response(request, data))
        finally:
            self.connection_closed.set()

    async def send_event(self, message_type: str, data: dict[str, object]) -> None:
        connection = self.connection
        assert connection is not None
        self.event_count += 1
        await connection.send(
            json.dumps(
                {
                    "apiName": "VTubeStudioPublicAPI",
                    "apiVersion": "1.0",
                    "requestID": f"synthetic-event-{self.event_count}",
                    "messageType": message_type,
                    "data": data,
                },
                separators=(",", ":"),
            )
        )


def _parameter_capabilities() -> list[dict[str, float | str]]:
    unit = {
        "min": -1.0,
        "max": 1.0,
        "defaultValue": 0.0,
    }
    positive = {
        "min": 0.0,
        "max": 1.0,
        "defaultValue": 1.0,
    }
    return [
        {"name": "MouthOpen", "min": 0.0, "max": 1.0, "defaultValue": 0.0},
        {"name": "MouthSmile", **unit},
        {"name": "Brows", **unit},
        {"name": "EyeOpenLeft", **positive},
        {"name": "EyeOpenRight", **positive},
        {"name": "EyeLeftX", **unit},
        {"name": "EyeLeftY", **unit},
        {"name": "EyeRightX", **unit},
        {"name": "EyeRightY", **unit},
        {
            "name": "FaceAngleX",
            "min": -30.0,
            "max": 30.0,
            "defaultValue": 0.0,
        },
        {
            "name": "FaceAngleY",
            "min": -30.0,
            "max": 30.0,
            "defaultValue": 0.0,
        },
        {
            "name": "FaceAngleZ",
            "min": -30.0,
            "max": 30.0,
            "defaultValue": 0.0,
        },
        {"name": "FacePositionY", **unit},
    ]


def _response(request: dict[str, Any], data: dict[str, Any]) -> str:
    request_type = cast(str, request["messageType"])
    return json.dumps(
        {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": request["requestID"],
            "messageType": f"{request_type.removesuffix('Request')}Response",
            "data": data,
        },
        separators=(",", ":"),
    )


def _uri(server: Server) -> str:
    assert server.sockets
    address = server.sockets[0].getsockname()
    assert isinstance(address, tuple)
    return f"ws://127.0.0.1:{address[1]}"


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


def _request_types(fake: _AvatarVTSServer) -> list[str]:
    return [cast(str, request["messageType"]) for request in fake.requests]


def _triggered_hotkeys(fake: _AvatarVTSServer) -> list[str]:
    return [
        cast(str, request["data"]["hotkeyID"])
        for request in fake.requests
        if request["messageType"] == "HotkeyTriggerRequest"
    ]


def _expression_activations(fake: _AvatarVTSServer) -> list[bool]:
    return [
        cast(bool, request["data"]["active"])
        for request in fake.requests
        if request["messageType"] == "ExpressionActivationRequest"
    ]


def test_runtime_uses_real_client_for_lifecycle_events_and_single_writer() -> None:
    async def scenario() -> None:
        fake = _AvatarVTSServer()
        server = await serve(fake.handler, "127.0.0.1", 0)
        runtime = AvatarRuntime(
            lambda: VTSClient(_uri(server), request_timeout_seconds=1.0),
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            config=AvatarConfig(
                tick_hz=25.0,
                red_eye_duration_seconds=0.08,
                release_hotkey_name="release_action",
                red_eye_hotkey_name="red_eye_toggle",
                red_eye_expression_file="red_eye.exp3.json",
                body_motion_hotkeys={
                    "happy": ("happy_action",),
                    "focused": ("focused_action",),
                },
            ),
            rng=random.Random(17),
        )
        runtime.start()
        try:
            await _wait_until(lambda: runtime.snapshot().state is AvatarRuntimeState.ready)

            request_types = _request_types(fake)
            assert "InputParameterListRequest" in request_types
            assert request_types.count("EventSubscriptionRequest") == 2
            assert "release-id" in _triggered_hotkeys(fake)
            assert _expression_activations(fake)[-1] is False

            happy_generation = runtime.begin_turn("turn_happy")
            assert happy_generation is not None
            assert runtime.set_turn_plan(
                map_avatar_turn_plan("turn_happy", EmotionLabel.happy),
                generation=happy_generation,
            )
            assert runtime.visual_fallback(
                "turn_happy",
                generation=happy_generation,
            )
            await _wait_until(lambda: _triggered_hotkeys(fake)[-1:] == ["happy-id"])
            assert runtime.complete_turn(
                "turn_happy",
                generation=happy_generation,
            )

            focused_generation = runtime.begin_turn("turn_focused")
            assert focused_generation is not None
            assert runtime.set_turn_plan(
                map_avatar_turn_plan("turn_focused", EmotionLabel.focused),
                generation=focused_generation,
            )
            assert runtime.visual_fallback(
                "turn_focused",
                generation=focused_generation,
            )
            await _wait_until(lambda: _triggered_hotkeys(fake)[-2:] == ["release-id", "focused-id"])

            activation_start = len(_expression_activations(fake))
            assert runtime.trigger_red_eye()
            await _wait_until(lambda: _expression_activations(fake)[activation_start:] == [True])
            assert runtime.trigger_red_eye()
            await _wait_until(
                lambda: _expression_activations(fake)[activation_start:] == [True, False],
                timeout=1.0,
            )
            assert runtime.red_eye_owner is RedEyeOwner.off

            fake.expression_active = True
            await fake.send_event(
                "HotkeyTriggeredEvent",
                {
                    "hotkeyName": "red_eye_toggle",
                    "hotkeyTriggeredByAPI": False,
                },
            )
            await _wait_until(lambda: runtime.red_eye_owner is RedEyeOwner.manual)
            manual_start = len(_expression_activations(fake))
            assert runtime.trigger_red_eye()
            await asyncio.sleep(0.12)
            assert len(_expression_activations(fake)) == manual_start

            fake.expression_active = False
            await fake.send_event(
                "HotkeyTriggeredEvent",
                {
                    "hotkeyName": "red_eye_toggle",
                    "hotkeyTriggeredByAPI": False,
                },
            )
            await _wait_until(lambda: runtime.red_eye_owner is RedEyeOwner.off)

            model_requests = _request_types(fake).count("CurrentModelRequest")
            await fake.send_event("ModelLoadedEvent", {"modelLoaded": True})
            await _wait_until(
                lambda: (
                    _request_types(fake).count("CurrentModelRequest") > model_requests
                    and runtime.snapshot().state is AvatarRuntimeState.ready
                )
            )
            assert not runtime.visual_fallback(
                "turn_focused",
                generation=focused_generation,
            )
            assert _triggered_hotkeys(fake)[-1] == "release-id"
            assert _expression_activations(fake)[-1] is False

            snapshot = runtime.snapshot()
            assert snapshot.sent_frames > 0
            assert "synthetic-private-model" not in repr(snapshot)
            assert "release-id" not in repr(snapshot)
            assert "synthetic-avatar-token" not in repr(snapshot)
        finally:
            requests_before_close = len(fake.requests)
            await runtime.close()
            await _wait_until(lambda: fake.connection_closed.is_set())
            assert len(fake.requests) > requests_before_close
            assert _triggered_hotkeys(fake)[-1] == "release-id"
            assert _expression_activations(fake)[-1] is False
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
