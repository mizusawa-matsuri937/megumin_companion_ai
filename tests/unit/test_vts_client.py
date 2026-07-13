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
