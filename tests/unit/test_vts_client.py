"""Loopback fake-WebSocket tests for VTS request correlation and failures."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Mapping
from typing import Any, cast

import pytest
from app.clients.vts import client as client_module
from app.clients.vts.client import (
    VTSAPIError,
    VTSClient,
    VTSConfigurationError,
    VTSConnectionError,
    VTSHotkeyTriggeredEvent,
    VTSModelLoadedEvent,
    VTSProtocolError,
    VTSRequestTimeout,
)
from websockets.asyncio.server import Server, ServerConnection, serve


def _uri(server: Server) -> str:
    assert server.sockets
    address = server.sockets[0].getsockname()
    assert isinstance(address, tuple)
    return f"ws://127.0.0.1:{address[1]}"


def _response(request: dict[str, Any], data: dict[str, Any]) -> str:
    request_type = cast(str, request["messageType"])
    return json.dumps(
        {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": request["requestID"],
            "messageType": f"{request_type.removesuffix('Request')}Response",
            "data": data,
        }
    )


class _StaticResponseClient(VTSClient):
    """Exercise response validators without weakening their public contract."""

    def __init__(self) -> None:
        super().__init__()
        self.response: dict[str, Any] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self,
        message_type: str,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.requests.append((message_type, dict(data or {})))
        return self.response


def test_client_rejects_invalid_timeout_and_disconnected_requests() -> None:
    with pytest.raises(ValueError, match="timeout"):
        VTSClient(request_timeout_seconds=0)

    async def scenario() -> None:
        client = VTSClient(request_timeout_seconds=0.1)
        assert not client.connected
        with pytest.raises(VTSConnectionError):
            await client.request("APIStateRequest")
        with pytest.raises(ValueError, match="hotkey"):
            await client.trigger_hotkey(" ")
        await client.close()

    asyncio.run(scenario())


def test_remote_wss_uses_verified_tls_and_ignores_implicit_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        captured: dict[str, object] = {}

        class Connection:
            async def send(self, _message: str) -> None:
                return None

            async def recv(self) -> str:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def close(self) -> None:
                return None

        async def connect(uri: str, **kwargs: object) -> Connection:
            captured["uri"] = uri
            captured.update(kwargs)
            return Connection()

        monkeypatch.setattr(client_module, "websocket_connect", connect)
        client = VTSClient("wss://vts.example/socket", proxy_url=None)
        await client.connect()
        await client.close()

        context = captured["ssl"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname
        assert captured["proxy"] is None
        assert "verify" not in captured

    asyncio.run(scenario())


def test_client_correlates_out_of_order_responses_from_fake_websocket() -> None:
    async def scenario() -> None:
        requests: list[dict[str, Any]] = []

        async def handler(connection: ServerConnection) -> None:
            requests.append(json.loads(await connection.recv()))
            requests.append(json.loads(await connection.recv()))
            await connection.send(
                json.dumps(
                    {
                        "requestID": "unsolicited",
                        "messageType": "APIStateResponse",
                        "data": {"ignored": True},
                    }
                )
            )
            for request in reversed(requests):
                await connection.send(_response(request, {"marker": request["data"]["marker"]}))

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            first = asyncio.create_task(client.request("FirstRequest", {"marker": "first"}))
            second = asyncio.create_task(client.request("SecondRequest", {"marker": "second"}))
            assert await first == {"marker": "first"}
            assert await second == {"marker": "second"}
            assert all(request["apiName"] == "VTubeStudioPublicAPI" for request in requests)
            assert len({request["requestID"] for request in requests}) == 2
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_client_authentication_helpers_and_api_error_do_not_expose_token() -> None:
    async def scenario() -> None:
        seen_token: str | None = None

        async def handler(connection: ServerConnection) -> None:
            nonlocal seen_token
            async for raw in connection:
                request = json.loads(raw)
                message_type = request["messageType"]
                if message_type == "AuthenticationTokenRequest":
                    data = {"authenticationToken": "local-secret-token"}
                    await connection.send(_response(request, data))
                elif message_type == "AuthenticationRequest":
                    seen_token = request["data"]["authenticationToken"]
                    await connection.send(_response(request, {"authenticated": True}))
                else:
                    await connection.send(
                        json.dumps(
                            {
                                "requestID": request["requestID"],
                                "messageType": "APIError",
                                "data": {"errorID": 200, "message": "missing hotkey"},
                            }
                        )
                    )

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            token = await client.request_token("Companion", "Local User")
            assert await client.authenticate("Companion", "Local User", token)
            assert seen_token == "local-secret-token"
            with pytest.raises(VTSAPIError, match="200") as error:
                await client.trigger_hotkey("missing")
            assert "local-secret-token" not in str(error.value)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_disconnect_fails_pending_request_and_signals_closed() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await connection.recv()
            await connection.close()

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            with pytest.raises(VTSConnectionError):
                await client.api_state()
            await asyncio.wait_for(client.wait_closed(), timeout=1)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_mismatched_response_type_is_rejected() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            request = json.loads(await connection.recv())
            await connection.send(
                json.dumps(
                    {
                        "requestID": request["requestID"],
                        "messageType": "WrongResponse",
                        "data": {},
                    }
                )
            )

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            with pytest.raises(VTSProtocolError):
                await client.api_state()
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_malformed_server_frame_is_classified_as_protocol_error() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await connection.recv()
            await connection.send("{malformed-json")

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            with pytest.raises(VTSProtocolError):
                await client.api_state()
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_fake_vts_server_validates_preflight_protocol_and_redacts_capabilities() -> None:
    async def scenario() -> None:
        requests: list[dict[str, Any]] = []

        async def handler(connection: ServerConnection) -> None:
            async for raw in connection:
                request = json.loads(raw)
                requests.append(request)
                message_type = request["messageType"]
                data: dict[str, Any]
                if message_type == "APIStateRequest":
                    data = {"active": True, "currentSessionAuthenticated": True}
                elif message_type == "CurrentModelRequest":
                    data = {
                        "modelLoaded": True,
                        "modelID": "private-model-id",
                        "live2DModelName": "private/user/model/path.model3.json",
                    }
                elif message_type == "HotkeysInCurrentModelRequest":
                    data = {
                        "availableHotkeys": [
                            {"hotkeyID": "neutral-key", "name": "private neutral name"},
                            {"hotkeyID": "happy-key", "file": "private-expression.exp3.json"},
                        ]
                    }
                else:
                    raise AssertionError(f"unexpected request: {message_type}")
                await connection.send(_response(request, data))

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            missing = await client.preflight(frozenset({"neutral-key", "happy-key", "missing-key"}))
            assert not missing.ready
            assert missing.error_code == "vts_hotkey_missing"
            assert missing.configured_hotkey_count == 3
            assert missing.missing_hotkey_count == 1
            assert "private" not in repr(missing)

            ready = await client.preflight(frozenset({"neutral-key", "happy-key"}))
            assert ready.ready
            assert ready.missing_hotkey_count == 0
            assert [request["messageType"] for request in requests] == [
                "APIStateRequest",
                "CurrentModelRequest",
                "HotkeysInCurrentModelRequest",
                "APIStateRequest",
                "CurrentModelRequest",
                "HotkeysInCurrentModelRequest",
            ]
            assert all(request["apiName"] == "VTubeStudioPublicAPI" for request in requests)
            assert all(request["apiVersion"] == "1.0" for request in requests)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("state", "model_loaded", "expected"),
    [
        ({"active": False, "currentSessionAuthenticated": False}, True, "vts_api_unavailable"),
        ({"active": True, "currentSessionAuthenticated": False}, True, "vts_auth_revoked"),
        ({"active": True, "currentSessionAuthenticated": True}, False, "vts_model_missing"),
    ],
)
def test_fake_vts_server_classifies_preflight_failures(
    state: dict[str, bool], model_loaded: bool, expected: str
) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            async for raw in connection:
                request = json.loads(raw)
                if request["messageType"] == "APIStateRequest":
                    data: dict[str, Any] = state
                elif request["messageType"] == "CurrentModelRequest":
                    data = {"modelLoaded": model_loaded}
                else:
                    data = {"availableHotkeys": [{"hotkeyID": "neutral-key"}]}
                await connection.send(_response(request, data))

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            result = await client.preflight(frozenset({"neutral-key"}))
            assert not result.ready
            assert result.error_code == expected
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


class _LifecycleConnection:
    def __init__(self) -> None:
        self.close_count = 0
        self._closed = asyncio.Event()

    async def send(self, _message: str) -> None:
        return None

    async def recv(self) -> str:
        await self._closed.wait()
        raise OSError("closed")

    async def close(self) -> None:
        self.close_count += 1
        self._closed.set()


class _FailingCloseConnection(_LifecycleConnection):
    async def close(self) -> None:
        await super().close()
        raise RuntimeError("close failed")


def test_connection_factory_error_is_mapped_and_retryable() -> None:
    async def scenario() -> None:
        factory_calls = 0

        async def factory(_uri: str) -> _LifecycleConnection:
            nonlocal factory_calls
            factory_calls += 1
            raise OSError("offline")

        client = VTSClient(connection_factory=factory)
        for _attempt in range(2):
            with pytest.raises(VTSConnectionError):
                await client.connect()
        assert factory_calls == 2
        await client.close()

    asyncio.run(scenario())


def test_close_cancels_inflight_connect_and_all_waiters_join_cleanup() -> None:
    async def scenario() -> None:
        factory_started = asyncio.Event()
        factory_cancelled = asyncio.Event()
        finish_factory_cleanup = asyncio.Event()
        connection = _LifecycleConnection()

        async def factory(_uri: str) -> _LifecycleConnection:
            factory_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A connection factory may need asynchronous cleanup and may
                # return a socket even after observing cancellation.
                factory_cancelled.set()
                await finish_factory_cleanup.wait()
                return connection
            raise AssertionError("blocking factory unexpectedly completed")

        client = VTSClient(connection_factory=factory)
        connecting = asyncio.create_task(client.connect())
        await asyncio.wait_for(factory_started.wait(), timeout=1)

        first_close = asyncio.create_task(client.close())
        await asyncio.wait_for(factory_cancelled.wait(), timeout=1)
        second_close = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        assert not first_close.done()
        assert not second_close.done()
        assert not client.connected

        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close
        assert not second_close.done()

        finish_factory_cleanup.set()
        await asyncio.wait_for(second_close, timeout=1)
        with pytest.raises(VTSConnectionError):
            await connecting

        assert connection.close_count == 1
        assert not client.connected
        await asyncio.wait_for(client.wait_closed(), timeout=1)
        await client.close()
        with pytest.raises(VTSConnectionError):
            await client.connect()

    asyncio.run(scenario())


def test_concurrent_connect_calls_share_one_connection_factory() -> None:
    async def scenario() -> None:
        factory_started = asyncio.Event()
        release_factory = asyncio.Event()
        factory_calls = 0
        connection = _LifecycleConnection()

        async def factory(_uri: str) -> _LifecycleConnection:
            nonlocal factory_calls
            factory_calls += 1
            factory_started.set()
            await release_factory.wait()
            return connection

        client = VTSClient(connection_factory=factory)
        first = asyncio.create_task(client.connect())
        await asyncio.wait_for(factory_started.wait(), timeout=1)
        second = asyncio.create_task(client.connect())
        await asyncio.sleep(0)
        release_factory.set()
        await asyncio.gather(first, second)

        assert factory_calls == 1
        assert client.connected
        await client.connect()
        assert factory_calls == 1
        await client.close()
        assert connection.close_count == 1

    asyncio.run(scenario())


def test_close_finishes_receiver_cleanup_before_reporting_socket_error() -> None:
    async def scenario() -> None:
        connection = _FailingCloseConnection()

        async def factory(_uri: str) -> _FailingCloseConnection:
            return connection

        client = VTSClient(connection_factory=factory)
        await client.connect()

        with pytest.raises(ExceptionGroup, match="connection close failed"):
            await client.close()
        assert not client.connected
        await asyncio.wait_for(client.wait_closed(), timeout=1)
        assert connection.close_count == 1

        with pytest.raises(ExceptionGroup, match="connection close failed"):
            await client.close()
        assert connection.close_count == 1

    asyncio.run(scenario())


def test_event_subscription_accepts_current_vts_subscription_list_response() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            subscribe = json.loads(await connection.recv())
            assert subscribe["messageType"] == "EventSubscriptionRequest"
            assert subscribe["data"] == {
                "eventName": "HotkeyTriggeredEvent",
                "subscribe": True,
                "config": {},
            }
            await connection.send(
                _response(
                    subscribe,
                    {
                        "subscribedEventCount": 2,
                        "subscribedEvents": ["FutureEvent", "HotkeyTriggeredEvent"],
                    },
                )
            )

            unsubscribe = json.loads(await connection.recv())
            assert unsubscribe["messageType"] == "EventSubscriptionRequest"
            assert unsubscribe["data"] == {
                "eventName": "HotkeyTriggeredEvent",
                "subscribe": False,
                "config": {},
            }
            await connection.send(
                _response(
                    unsubscribe,
                    {
                        "subscribedEventCount": 1,
                        "subscribedEvents": ["FutureEvent"],
                    },
                )
            )

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            await client.subscribe_event("HotkeyTriggeredEvent")
            await client.subscribe_event("HotkeyTriggeredEvent", subscribe=False)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("data", "subscribe"),
    [
        ({"subscribedEventCount": 0}, True),
        ({"subscribedEvents": []}, True),
        (
            {
                "subscribedEventCount": True,
                "subscribedEvents": ["HotkeyTriggeredEvent"],
            },
            True,
        ),
        (
            {
                "subscribedEventCount": -1,
                "subscribedEvents": ["HotkeyTriggeredEvent"],
            },
            True,
        ),
        (
            {
                "subscribedEventCount": 2049,
                "subscribedEvents": ["HotkeyTriggeredEvent"],
            },
            True,
        ),
        (
            {
                "subscribedEventCount": 1,
                "subscribedEvents": "HotkeyTriggeredEvent",
            },
            True,
        ),
        (
            {
                "subscribedEventCount": 2,
                "subscribedEvents": ["HotkeyTriggeredEvent"],
            },
            True,
        ),
        (
            {
                "subscribedEventCount": 2,
                "subscribedEvents": [
                    "HotkeyTriggeredEvent",
                    "HotkeyTriggeredEvent",
                ],
            },
            True,
        ),
        (
            {
                "subscribedEventCount": 1,
                "subscribedEvents": [1],
            },
            True,
        ),
        (
            {
                "subscribedEventCount": 1,
                "subscribedEvents": [""],
            },
            True,
        ),
        (
            {
                "subscribedEventCount": 1,
                "subscribedEvents": ["ModelLoadedEvent"],
            },
            True,
        ),
        (
            {
                "subscribedEventCount": 1,
                "subscribedEvents": ["HotkeyTriggeredEvent"],
            },
            False,
        ),
    ],
)
def test_event_subscription_rejects_malformed_or_inconsistent_lists(
    data: dict[str, Any],
    subscribe: bool,
) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            request = json.loads(await connection.recv())
            await connection.send(_response(request, data))

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            with pytest.raises(VTSProtocolError):
                await client.subscribe_event(
                    "HotkeyTriggeredEvent",
                    subscribe=subscribe,
                )
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_client_routes_unsolicited_events_without_confusing_pending_requests() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            subscription = json.loads(await connection.recv())
            assert subscription["messageType"] == "EventSubscriptionRequest"
            await connection.send(
                _response(
                    subscription,
                    {
                        "subscribedEventCount": 1,
                        "subscribedEvents": ["HotkeyTriggeredEvent"],
                    },
                )
            )
            await connection.send(
                json.dumps(
                    {
                        "apiName": "VTubeStudioPublicAPI",
                        "apiVersion": "1.0",
                        "timestamp": 123,
                        "requestID": "vts-generated-event-id",
                        "messageType": "HotkeyTriggeredEvent",
                        "data": {
                            "hotkeyID": "private-id-must-not-leave-client",
                            "hotkeyName": "FX_RedEye",
                            "hotkeyType": "ToggleExpression",
                            "hotkeyTriggeredByAPI": False,
                            "modelID": "private-model-id",
                        },
                    }
                )
            )
            request = json.loads(await connection.recv())
            await connection.send(_response(request, {"active": True}))
            await connection.send(
                json.dumps(
                    {
                        "apiName": "VTubeStudioPublicAPI",
                        "apiVersion": "1.0",
                        "timestamp": 124,
                        "requestID": "vts-generated-model-event-id",
                        "messageType": "ModelLoadedEvent",
                        "data": {
                            "modelLoaded": True,
                            "modelID": "another-private-model-id",
                            "modelName": "private-model-name",
                        },
                    }
                )
            )

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            await client.subscribe_event("HotkeyTriggeredEvent")
            hotkey = await asyncio.wait_for(client.next_event(), timeout=1)
            assert hotkey == VTSHotkeyTriggeredEvent(
                hotkey_name="FX_RedEye",
                triggered_by_api=False,
            )
            assert await client.api_state() == {"active": True}
            model = await asyncio.wait_for(client.next_event(), timeout=1)
            assert model == VTSModelLoadedEvent(model_loaded=True)
            assert "private" not in repr(hotkey)
            assert "private" not in repr(model)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_client_routes_event_even_when_vts_reuses_a_pending_request_id() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            subscription = json.loads(await connection.recv())
            await connection.send(
                _response(
                    subscription,
                    {
                        "subscribedEventCount": 1,
                        "subscribedEvents": ["HotkeyTriggeredEvent"],
                    },
                )
            )
            request = json.loads(await connection.recv())
            assert request["messageType"] == "APIStateRequest"
            await connection.send(
                json.dumps(
                    {
                        "apiName": "VTubeStudioPublicAPI",
                        "apiVersion": "1.0",
                        "requestID": request["requestID"],
                        "messageType": "HotkeyTriggeredEvent",
                        "data": {
                            "hotkeyName": "FX_RedEye",
                            "hotkeyTriggeredByAPI": False,
                        },
                    }
                )
            )
            await connection.send(_response(request, {"active": True}))

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            await client.subscribe_event("HotkeyTriggeredEvent")
            response = asyncio.create_task(client.api_state())
            assert await asyncio.wait_for(client.next_event(), timeout=1) == (
                VTSHotkeyTriggeredEvent(
                    hotkey_name="FX_RedEye",
                    triggered_by_api=False,
                )
            )
            assert await response == {"active": True}
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("request_id", [None, 1, "", "x" * 257])
def test_client_rejects_malformed_event_request_id(request_id: object) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await connection.recv()
            await connection.send(
                json.dumps(
                    {
                        "apiName": "VTubeStudioPublicAPI",
                        "apiVersion": "1.0",
                        "requestID": request_id,
                        "messageType": "HotkeyTriggeredEvent",
                        "data": {
                            "hotkeyName": "FX_RedEye",
                            "hotkeyTriggeredByAPI": False,
                        },
                    }
                )
            )

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            with pytest.raises(VTSProtocolError):
                await client.api_state()
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_client_validates_parameter_expression_and_unique_hotkey_apis() -> None:
    async def scenario() -> None:
        requests: list[dict[str, Any]] = []

        async def handler(connection: ServerConnection) -> None:
            async for raw in connection:
                request = json.loads(raw)
                requests.append(request)
                message_type = request["messageType"]
                data: dict[str, Any]
                if message_type == "InputParameterListRequest":
                    data = {
                        "defaultParameters": [
                            {
                                "name": "MouthOpen",
                                "addedBy": "VTube Studio",
                                "value": 0.0,
                                "min": 0.0,
                                "max": 1.0,
                                "defaultValue": 0.0,
                            },
                            {
                                "name": "FaceAngleX",
                                "addedBy": "VTube Studio",
                                "value": 0.0,
                                "min": -30.0,
                                "max": 30.0,
                                "defaultValue": 0.0,
                            },
                        ],
                        "customParameters": [],
                    }
                elif message_type == "CurrentModelRequest":
                    data = {
                        "modelLoaded": True,
                        "modelName": "private-current-model",
                    }
                elif message_type == "HotkeysInCurrentModelRequest":
                    data = {
                        "availableHotkeys": [
                            {"hotkeyID": "private-release-id", "name": "MOTION_RELEASE"},
                            {"hotkeyID": "private-fx-id", "name": "FX_RedEye"},
                        ]
                    }
                elif message_type == "InjectParameterDataRequest":
                    data = {}
                elif message_type == "ExpressionStateRequest":
                    data = {
                        "expressions": [
                            {
                                "name": "FX_RedEye",
                                "file": "FX_RedEye.exp3.json",
                                "active": True,
                                "deactivateWhenKeyIsLetGo": False,
                                "autoDeactivateAfterSeconds": False,
                                "secondsRemaining": 0.0,
                            }
                        ]
                    }
                elif message_type == "ExpressionActivationRequest":
                    data = {}
                else:
                    raise AssertionError(message_type)
                await connection.send(_response(request, data))

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            assert await client.current_model_name() == "private-current-model"
            capabilities = await client.parameter_capabilities()
            assert set(capabilities) == {"MouthOpen", "FaceAngleX"}
            assert capabilities["MouthOpen"].minimum == 0.0
            assert capabilities["FaceAngleX"].maximum == 30.0
            assert await client.resolve_hotkey_name("motion_release") == "private-release-id"
            await client.inject_parameter_values(
                {"MouthOpen": 0.75, "FaceAngleX": -2.0},
                face_found=True,
            )
            assert await client.expression_is_active("FX_RedEye.exp3.json")
            await client.set_expression_active(
                "FX_RedEye.exp3.json",
                active=False,
                fade_seconds=0.1,
            )
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

        injected = next(
            request
            for request in requests
            if request["messageType"] == "InjectParameterDataRequest"
        )
        assert injected["data"] == {
            "faceFound": True,
            "mode": "set",
            "parameterValues": [
                {"id": "MouthOpen", "value": 0.75, "weight": 1.0},
                {"id": "FaceAngleX", "value": -2.0, "weight": 1.0},
            ],
        }
        activation = next(
            request
            for request in requests
            if request["messageType"] == "ExpressionActivationRequest"
        )
        assert activation["data"] == {
            "expressionFile": "FX_RedEye.exp3.json",
            "active": False,
            "fadeTime": 0.1,
        }

    asyncio.run(scenario())


def test_hotkey_resolution_normalizes_vts_boundary_whitespace_but_stays_unique() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            async for raw in connection:
                request = json.loads(raw)
                assert request["messageType"] == "HotkeysInCurrentModelRequest"
                await connection.send(
                    _response(
                        request,
                        {
                            "availableHotkeys": [
                                {
                                    "hotkeyID": "private-doubt-id",
                                    "name": "MOTION_DOUBT_01 ",
                                },
                                {
                                    "hotkeyID": "private-first-duplicate-id",
                                    "name": "MOTION_DUPLICATE",
                                },
                                {
                                    "hotkeyID": "private-second-duplicate-id",
                                    "name": " motion_duplicate ",
                                },
                            ]
                        },
                    )
                )

        server = await serve(handler, "127.0.0.1", 0)
        client = VTSClient(_uri(server), request_timeout_seconds=1)
        try:
            await client.connect()
            assert await client.resolve_hotkey_name("motion_doubt_01") == "private-doubt-id"
            with pytest.raises(VTSConfigurationError) as caught:
                await client.resolve_hotkey_name("motion_duplicate")
            assert caught.value.code == "vts_hotkey_ambiguous"
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_server_decoder_rejects_bounded_json_edge_cases() -> None:
    deep_value: object = 0
    for _ in range(client_module._MAX_SERVER_DEPTH + 2):
        deep_value = {"child": deep_value}

    oversized_collection = {"value": list(range(client_module._MAX_SERVER_COLLECTION_ITEMS + 1))}
    oversized_mapping = {
        "value": {
            f"key_{index}": index for index in range(client_module._MAX_SERVER_COLLECTION_ITEMS + 1)
        }
    }
    oversized_frame = json.dumps({"value": "x" * (client_module._MAX_SERVER_MESSAGE_BYTES + 1)})
    malformed_frames = (
        '{"value":NaN}',
        '{"duplicate":1,"duplicate":2}',
        json.dumps({"value": 2**53 + 1}),
        json.dumps({"value": "x" * (client_module._MAX_SERVER_STRING_CHARS + 1)}),
        '{"value":"\\u0000"}',
        '{"value":"\\ud800"}',
        json.dumps(oversized_collection),
        json.dumps(oversized_mapping),
        json.dumps(deep_value),
        '{"":1}',
        "[]",
        oversized_frame,
    )

    for raw in malformed_frames:
        with pytest.raises(VTSProtocolError):
            client_module._decode_server_message(raw)

    for value in (float("inf"), {1: "invalid-key"}, object()):
        with pytest.raises(VTSProtocolError):
            client_module._validate_server_value(value)


def test_parameter_capability_protocol_rejects_malformed_collections() -> None:
    async def scenario() -> None:
        client = _StaticResponseClient()

        def parameter(
            name: object = "MouthOpen",
            minimum: object = 0.0,
            maximum: object = 1.0,
            default: object = 0.0,
        ) -> dict[str, object]:
            return {
                "name": name,
                "min": minimum,
                "max": maximum,
                "defaultValue": default,
            }

        malformed_responses: tuple[dict[str, Any], ...] = (
            {"defaultParameters": None, "customParameters": []},
            {
                "defaultParameters": [
                    parameter() for _ in range(client_module._MAX_PARAMETER_COUNT + 1)
                ],
                "customParameters": [],
            },
            {"defaultParameters": [None], "customParameters": []},
            {
                "defaultParameters": [parameter(minimum=True)],
                "customParameters": [],
            },
            {
                "defaultParameters": [parameter(minimum=2.0, maximum=1.0, default=1.5)],
                "customParameters": [],
            },
            {
                "defaultParameters": [parameter()],
                "customParameters": [parameter()],
            },
        )
        for response in malformed_responses:
            client.response = response
            with pytest.raises(VTSProtocolError):
                await client.parameter_capabilities()

    asyncio.run(scenario())


def test_parameter_expression_and_event_boundaries_fail_closed() -> None:
    async def scenario() -> None:
        client = _StaticResponseClient()

        invalid_parameter_frames: tuple[Mapping[str, float], ...] = (
            {},
            {
                f"Parameter{index}": 0.0
                for index in range(client_module._MAX_INJECTED_PARAMETERS + 1)
            },
            {"bad\x00id": 0.0},
            {"MouthOpen": cast(float, True)},
            {"MouthOpen": float("nan")},
            {"MouthOpen": 1_000_001.0},
        )
        for values in invalid_parameter_frames:
            with pytest.raises(ValueError, match="parameter"):
                await client.inject_parameter_values(values, face_found=True)

        malformed_expression_responses = (
            {"expressions": None},
            {"expressions": [None]},
            {"expressions": [{"file": "FX.exp3.json", "active": 1}]},
        )
        for response in malformed_expression_responses:
            client.response = response
            with pytest.raises(VTSProtocolError):
                await client.expression_is_active("FX.exp3.json")

        client.response = {"expressions": []}
        with pytest.raises(VTSConfigurationError) as missing:
            await client.expression_is_active("FX.exp3.json")
        assert missing.value.code == "vts_hotkey_missing"

        client.response = {
            "expressions": [
                {"file": "FX.exp3.json", "active": True},
                {"file": "fx.exp3.json", "active": False},
            ]
        }
        with pytest.raises(VTSConfigurationError) as ambiguous:
            await client.expression_is_active("FX.exp3.json")
        assert ambiguous.value.code == "vts_hotkey_ambiguous"

        for expression_file in ("", "../FX.exp3.json", "FX.motion3.json"):
            with pytest.raises(ValueError, match="expression"):
                await client.set_expression_active(
                    expression_file,
                    active=True,
                    fade_seconds=0.1,
                )

        invalid_activations = (
            (cast(bool, 1), 0.1),
            (True, float("nan")),
            (True, -0.1),
            (True, 2.1),
        )
        for active, fade_seconds in invalid_activations:
            with pytest.raises(ValueError, match="activation"):
                await client.set_expression_active(
                    "FX.exp3.json",
                    active=active,
                    fade_seconds=fade_seconds,
                )

        with pytest.raises(ValueError, match="subscription"):
            await client.subscribe_event("UnknownEvent")
        with pytest.raises(ValueError, match="subscription"):
            await client.subscribe_event(
                "ModelLoadedEvent",
                subscribe=cast(bool, 1),
            )

    asyncio.run(scenario())


def test_event_mailbox_is_bounded_and_latest_wins() -> None:
    async def scenario() -> None:
        client = VTSClient()
        for index in range(client_module._EVENT_QUEUE_CAPACITY + 1):
            client._offer_event(VTSModelLoadedEvent(model_loaded=bool(index % 2)))

        assert client.dropped_event_count == 1
        assert client._events.qsize() == client_module._EVENT_QUEUE_CAPACITY
        observed = [await client.next_event() for _ in range(client_module._EVENT_QUEUE_CAPACITY)]
        assert observed[0] == VTSModelLoadedEvent(model_loaded=True)
        assert observed[-1] == VTSModelLoadedEvent(model_loaded=False)

    asyncio.run(scenario())


def test_content_free_response_and_terminal_boundaries_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        VTSConfigurationError("unsupported")

    # A valid explicit proxy exercises constructor validation without opening a socket.
    VTSClient(proxy_url="https://proxy.example:443")

    async def scenario() -> None:
        client = _StaticResponseClient()

        client.response = {"authenticationToken": ""}
        with pytest.raises(VTSProtocolError):
            await client.request_token("Companion", "Local User")

        client.response = {"authenticated": 1}
        with pytest.raises(VTSProtocolError):
            await client.authenticate("Companion", "Local User", "synthetic-token")

        client.response = {"modelLoaded": "yes", "modelName": "synthetic"}
        with pytest.raises(VTSProtocolError):
            await client.current_model_name()
        client.response = {"modelLoaded": False}
        with pytest.raises(VTSConfigurationError) as missing_model:
            await client.current_model_name()
        assert missing_model.value.code == "vts_model_missing"

        for response in (
            {"availableHotkeys": None},
            {"availableHotkeys": [None]},
        ):
            client.response = response
            with pytest.raises(VTSProtocolError):
                await client.available_hotkey_ids()
        client.response = {"availableHotkeys": []}
        with pytest.raises(VTSConfigurationError) as missing_hotkey:
            await client.resolve_hotkey_name("synthetic_release")
        assert missing_hotkey.value.code == "vts_hotkey_missing"

        client.response = {"active": 1, "currentSessionAuthenticated": True}
        with pytest.raises(VTSProtocolError):
            await client.preflight(frozenset())

        class _MalformedModelPreflightClient(_StaticResponseClient):
            async def api_state(self) -> dict[str, Any]:
                return {"active": True, "currentSessionAuthenticated": True}

            async def current_model(self) -> dict[str, Any]:
                return {"modelLoaded": "yes"}

        with pytest.raises(VTSProtocolError):
            await _MalformedModelPreflightClient().preflight(frozenset())

        await client.close()
        for _ in range(2):
            with pytest.raises(VTSConnectionError):
                await client.next_event()

    asyncio.run(scenario())


def test_dispatch_and_request_timeout_boundaries_fail_closed() -> None:
    async def scenario() -> None:
        client = VTSClient()
        client._dispatch(
            json.dumps(
                {
                    "apiName": "VTubeStudioPublicAPI",
                    "apiVersion": "1.0",
                    "messageType": "UnknownEvent",
                    "data": {},
                }
            )
        )
        with pytest.raises(VTSProtocolError):
            client._dispatch(
                json.dumps(
                    {
                        "requestID": 1,
                        "messageType": "APIStateResponse",
                        "data": {},
                    }
                )
            )

        done_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        done_future.set_result({})
        client._pending["done"] = ("APIStateResponse", done_future)
        client._dispatch(
            json.dumps(
                {
                    "requestID": "done",
                    "messageType": "APIStateResponse",
                    "data": {},
                }
            )
        )

        malformed_future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        client._pending["malformed"] = ("APIStateResponse", malformed_future)
        client._dispatch(
            json.dumps(
                {
                    "requestID": "malformed",
                    "messageType": "APIStateResponse",
                    "data": [],
                }
            )
        )
        assert isinstance(malformed_future.exception(), VTSProtocolError)

        malformed_events = (
            {
                "apiName": "wrong",
                "apiVersion": "1.0",
                "requestID": "event",
                "messageType": "ModelLoadedEvent",
                "data": {"modelLoaded": True},
            },
            {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "event",
                "messageType": "ModelLoadedEvent",
                "data": [],
            },
            {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "event",
                "messageType": "HotkeyTriggeredEvent",
                "data": {
                    "hotkeyName": "synthetic_red_eye",
                    "hotkeyTriggeredByAPI": 1,
                },
            },
            {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "event",
                "messageType": "ModelLoadedEvent",
                "data": {"modelLoaded": 1},
            },
        )
        for event in malformed_events:
            with pytest.raises(VTSProtocolError):
                client._dispatch(json.dumps(event))

        await client.close()

        async def handler(connection: ServerConnection) -> None:
            await connection.recv()
            await asyncio.sleep(1)

        server = await serve(handler, "127.0.0.1", 0)
        timeout_client = VTSClient(_uri(server), request_timeout_seconds=0.01)
        try:
            await timeout_client.connect()
            with pytest.raises(VTSRequestTimeout):
                await timeout_client.api_state()
        finally:
            await timeout_client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
