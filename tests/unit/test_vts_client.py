"""Loopback fake-WebSocket tests for VTS request correlation and failures."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from app.clients.vts.client import (
    VTSAPIError,
    VTSClient,
    VTSConnectionError,
    VTSProtocolError,
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
