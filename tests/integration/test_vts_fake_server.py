"""End-to-end VTS bridge tests against a loopback protocol server."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from app.clients.vts import (
    ExpressionMapper,
    VTSBridge,
    VTSBridgeState,
    VTSClient,
    VTSToken,
    VTSTurnEventSink,
)
from app.core import CancellationToken, TurnService
from app.schemas import TurnMetrics, TurnOutcome, TurnState, UserMessage
from websockets.asyncio.server import Server, ServerConnection, serve


class _TokenStore:
    def __init__(self) -> None:
        self.token: VTSToken | None = VTSToken(
            "Companion", "Local User", "fake-server-secret-token"
        )

    async def load(self) -> VTSToken | None:
        return self.token

    async def save(self, token: VTSToken) -> None:
        self.token = token

    async def delete(self) -> None:
        self.token = None


class _FakeClock:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self._permits: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        await self._permits.get()

    def advance(self) -> None:
        self._permits.put_nowait(None)


class _FakeVTSServer:
    def __init__(self) -> None:
        self.connections = 0
        self.requests: list[dict[str, Any]] = []
        self.triggered: list[tuple[int, str]] = []

    async def handler(self, connection: ServerConnection) -> None:
        self.connections += 1
        connection_number = self.connections
        authenticated = False
        async for raw in connection:
            request = json.loads(raw)
            self.requests.append(request)
            message_type = request["messageType"]
            data: dict[str, Any]
            if message_type == "APIStateRequest":
                data = {"active": True, "currentSessionAuthenticated": authenticated}
            elif message_type == "AuthenticationRequest":
                authenticated = (
                    request["data"].get("authenticationToken") == "fake-server-secret-token"
                )
                data = {"authenticated": authenticated, "reason": "fixture"}
            elif message_type == "CurrentModelRequest":
                data = {"modelLoaded": True, "modelID": "fixture-private-model"}
            elif message_type == "HotkeysInCurrentModelRequest":
                data = {
                    "modelLoaded": True,
                    "availableHotkeys": [
                        {"hotkeyID": "neutral-key"},
                        {"hotkeyID": "happy-key"},
                    ],
                }
            elif message_type == "HotkeyTriggerRequest":
                hotkey_id = cast(str, request["data"]["hotkeyID"])
                self.triggered.append((connection_number, hotkey_id))
                data = {"hotkeyID": hotkey_id}
            else:
                raise AssertionError(f"unsupported fake VTS request: {message_type}")
            await connection.send(_response(request, data))
            if (
                connection_number == 1
                and message_type == "HotkeyTriggerRequest"
                and request["data"]["hotkeyID"] == "neutral-key"
            ):
                await connection.close()
                return


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


def _uri(server: Server) -> str:
    assert server.sockets
    address = server.sockets[0].getsockname()
    assert isinstance(address, tuple)
    return f"ws://127.0.0.1:{address[1]}"


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


def test_bridge_protocol_preflight_disconnect_and_reconnect_with_fake_vts_server() -> None:
    async def scenario() -> None:
        fake = _FakeVTSServer()
        server = await serve(fake.handler, "127.0.0.1", 0)
        clock = _FakeClock()
        bridge = VTSBridge(
            lambda: VTSClient(_uri(server), request_timeout_seconds=1),
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=ExpressionMapper({"neutral": "neutral-key", "happy": "happy-key"}),
            reconnect_initial_seconds=1,
            reconnect_max_seconds=4,
            sleep=clock.sleep,
            random_source=lambda: 0.0,
        )
        old_generation = bridge.begin_turn("turn-old")
        assert old_generation is not None
        assert bridge.enqueue_expression("happy", turn_id="turn-old", generation=old_generation)
        bridge.start()
        try:
            await _wait_until(lambda: len(clock.delays) == 1)
            assert fake.triggered == [(1, "neutral-key")]
            assert bridge.snapshot().state is VTSBridgeState.backoff
            assert bridge.snapshot().preflight_ready is False

            clock.advance()
            await _wait_until(
                lambda: (
                    fake.connections == 2
                    and fake.triggered[-1:] == [(2, "neutral-key")]
                    and bridge.snapshot().state is VTSBridgeState.ready
                )
            )
            assert (2, "happy-key") not in fake.triggered

            generation = bridge.begin_turn("turn-new")
            assert generation is not None and generation > old_generation
            assert bridge.enqueue_expression("happy", turn_id="turn-new", generation=generation)
            await _wait_until(lambda: fake.triggered[-2:] == [(2, "neutral-key"), (2, "happy-key")])

            request_types = [request["messageType"] for request in fake.requests]
            assert request_types.count("AuthenticationRequest") == 2
            assert request_types.count("CurrentModelRequest") == 2
            assert request_types.count("HotkeysInCurrentModelRequest") == 2
            assert "fake-server-secret-token" not in repr(bridge.snapshot())
            assert "fixture-private-model" not in repr(bridge.snapshot())
        finally:
            await bridge.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_model_preflight_failure_disables_expressions_but_dialogue_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    class DialoguePipeline:
        closed = False

        async def run(
            self,
            _message: UserMessage,
            _state: TurnState,
            _token: CancellationToken,
            emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        ) -> TurnOutcome:
            await emit(
                "assistant.segment",
                {
                    "text": "private-dialogue-sentinel",
                    "live2d_expression": "happy",
                },
            )
            return TurnOutcome(
                full_text="private-dialogue-sentinel",
                segments=[],
                metrics=TurnMetrics(segment_count=1),
            )

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        requests: list[dict[str, Any]] = []

        async def no_model_handler(connection: ServerConnection) -> None:
            authenticated = False
            async for raw in connection:
                request = json.loads(raw)
                requests.append(request)
                message_type = request["messageType"]
                data: dict[str, Any]
                if message_type == "APIStateRequest":
                    data = {"active": True, "currentSessionAuthenticated": authenticated}
                elif message_type == "AuthenticationRequest":
                    authenticated = True
                    data = {"authenticated": True}
                elif message_type == "CurrentModelRequest":
                    data = {
                        "modelLoaded": False,
                        "modelID": "private-model-sentinel",
                        "live2DModelName": "C:/Users/private/model.model3.json",
                    }
                else:
                    raise AssertionError(message_type)
                await connection.send(_response(request, data))

        server = await serve(no_model_handler, "127.0.0.1", 0)
        bridge = VTSBridge(
            lambda: VTSClient(_uri(server), request_timeout_seconds=1),
            _TokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=ExpressionMapper({"neutral": "neutral-key", "happy": "happy-key"}),
        )
        sink = VTSTurnEventSink(bridge)
        sink.start()
        pipeline = DialoguePipeline()
        service = TurnService(
            logging.getLogger("test.w09.dialogue-isolation"),
            pipeline,
            event_sinks=(sink,),
        )
        try:
            await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.disabled)
            state = await service.accept(UserMessage(text="private-user-body"))
            await service.wait_idle()
            assert service.snapshot()["turns"][state.turn_id]["status"] == "completed"
            assert bridge.snapshot().error_code == "vts_model_missing"
            assert bridge.snapshot().queue_size == 0

            diagnostic = repr(bridge.snapshot())
            assert "fake-server-secret-token" not in diagnostic
            assert "private-dialogue-sentinel" not in diagnostic
            assert "private-model-sentinel" not in diagnostic
            assert "C:/Users/private" not in diagnostic
            assert "private-dialogue-sentinel" not in json.dumps(requests)
            assert "private-user-body" not in json.dumps(requests)
            serialized_logs = "\n".join(
                f"{record.getMessage()} {record.__dict__!r}"
                for record in caplog.records
                if not record.name.startswith("websockets.server")
            )
            for sentinel in (
                "fake-server-secret-token",
                "private-dialogue-sentinel",
                "private-user-body",
                "private-model-sentinel",
                "C:/Users/private",
            ):
                assert sentinel not in serialized_logs
        finally:
            await service.shutdown()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
