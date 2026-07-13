"""Minimal correlated client for the VTube Studio public WebSocket API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast
from uuid import uuid4

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

VTS_API_NAME = "VTubeStudioPublicAPI"
VTS_API_VERSION = "1.0"


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[str], Awaitable[WebSocketConnection]]


class VTSError(RuntimeError):
    """Base error with a stable, non-secret code."""

    code = "vts_error"


class VTSConnectionError(VTSError):
    code = "vts_disconnected"


class VTSProtocolError(VTSError):
    code = "vts_protocol_error"


class VTSRequestTimeout(VTSError):
    code = "vts_timeout"


class VTSAPIError(VTSError):
    code = "vts_api_error"

    def __init__(self, error_id: int | None) -> None:
        super().__init__(f"VTS API error ({error_id if error_id is not None else 'unknown'})")
        self.error_id = error_id


async def _default_connection_factory(uri: str) -> WebSocketConnection:
    connection = await websocket_connect(
        uri,
        open_timeout=5,
        close_timeout=3,
        ping_interval=20,
        ping_timeout=10,
        max_size=1024 * 1024,
        max_queue=16,
    )
    return cast(WebSocketConnection, connection)


class VTSClient:
    def __init__(
        self,
        uri: str = "ws://127.0.0.1:8001",
        *,
        request_timeout_seconds: float = 5.0,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if request_timeout_seconds <= 0.0:
            raise ValueError("request_timeout_seconds 必须大于 0")
        self._uri = uri
        self._request_timeout = request_timeout_seconds
        self._connection_factory = connection_factory or _default_connection_factory
        self._connection: WebSocketConnection | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[WebSocketConnection] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._pending: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._send_lock = asyncio.Lock()
        self._closed_event = asyncio.Event()
        self._closed_event.set()
        self._closed = False

    @property
    def connected(self) -> bool:
        return not self._closed and self._connection is not None and not self._closed_event.is_set()

    async def connect(self) -> None:
        if self._closed:
            raise VTSConnectionError
        if self.connected:
            return
        task = self._connect_task
        if task is None:
            task = asyncio.create_task(
                self._open_connection(),
                name="vts-connect",
            )
            self._connect_task = task
        try:
            connection = await asyncio.shield(task)
        except (OSError, TimeoutError, ConnectionClosed) as exc:
            if self._connect_task is task:
                self._connect_task = None
            raise VTSConnectionError from exc
        except BaseException:
            if task.done() and self._connect_task is task:
                self._connect_task = None
            raise
        if self._closed:
            # The shared close task owns the connection returned by an in-flight
            # factory and will close it before returning.
            raise VTSConnectionError
        if self._connection is not None and not self._closed_event.is_set():
            return
        if self._connect_task is task:
            self._connect_task = None
        self._connection = connection
        self._closed_event.clear()
        self._receiver = asyncio.create_task(self._receive_loop(), name="vts-receiver")

    async def _open_connection(self) -> WebSocketConnection:
        return await self._connection_factory(self._uri)

    async def request(
        self,
        message_type: str,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        connection = self._connection
        if connection is None or not self.connected:
            raise VTSConnectionError
        request_id = uuid4().hex
        envelope = {
            "apiName": VTS_API_NAME,
            "apiVersion": VTS_API_VERSION,
            "requestID": request_id,
            "messageType": message_type,
            "data": dict(data or {}),
        }
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        response_type = f"{message_type.removesuffix('Request')}Response"
        self._pending[request_id] = (response_type, future)
        try:
            async with self._send_lock:
                await connection.send(
                    json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
                )
            return await asyncio.wait_for(future, timeout=self._request_timeout)
        except TimeoutError as exc:
            raise VTSRequestTimeout from exc
        except (OSError, ConnectionClosed) as exc:
            raise VTSConnectionError from exc
        finally:
            self._pending.pop(request_id, None)

    async def api_state(self) -> dict[str, Any]:
        return await self.request("APIStateRequest")

    async def request_token(self, plugin_name: str, plugin_developer: str) -> str:
        data = await self.request(
            "AuthenticationTokenRequest",
            {"pluginName": plugin_name, "pluginDeveloper": plugin_developer},
        )
        value = data.get("authenticationToken")
        if not isinstance(value, str) or not value:
            raise VTSProtocolError
        return value

    async def authenticate(
        self,
        plugin_name: str,
        plugin_developer: str,
        authentication_token: str,
    ) -> bool:
        data = await self.request(
            "AuthenticationRequest",
            {
                "pluginName": plugin_name,
                "pluginDeveloper": plugin_developer,
                "authenticationToken": authentication_token,
            },
        )
        authenticated = data.get("authenticated")
        if not isinstance(authenticated, bool):
            raise VTSProtocolError
        return authenticated

    async def trigger_hotkey(self, hotkey_id: str) -> None:
        if not hotkey_id.strip():
            raise ValueError("hotkey_id 不能为空")
        await self.request("HotkeyTriggerRequest", {"hotkeyID": hotkey_id})

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    async def _receive_loop(self) -> None:
        connection = self._connection
        assert connection is not None
        failure: Exception = VTSConnectionError()
        try:
            while True:
                raw = await connection.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                self._dispatch(raw)
        except asyncio.CancelledError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ConnectionClosed, VTSProtocolError):
            pass
        finally:
            if self._connection is connection:
                self._connection = None
            self._closed_event.set()
            for _response_type, future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(failure)

    def _dispatch(self, raw: str) -> None:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise VTSProtocolError
        request_id = payload.get("requestID")
        if not isinstance(request_id, str):
            return
        pending = self._pending.get(request_id)
        if pending is None:
            return
        response_type, future = pending
        if future.done():
            return
        message_type = payload.get("messageType")
        data = payload.get("data")
        if message_type == "APIError":
            error_id = data.get("errorID") if isinstance(data, dict) else None
            future.set_exception(VTSAPIError(error_id if isinstance(error_id, int) else None))
            return
        if message_type != response_type:
            future.set_exception(VTSProtocolError())
            return
        if not isinstance(data, dict):
            future.set_exception(VTSProtocolError())
            return
        future.set_result(data)

    async def close(self) -> None:
        task = self._close_task
        if task is None:
            # Publish the terminal state before yielding so no new connection
            # can be installed after shutdown starts.
            self._closed = True
            task = asyncio.create_task(self._close(), name="vts-client-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close(self) -> None:
        connecting = self._connect_task
        pending_connection: WebSocketConnection | None = None
        if connecting is not None:
            if not connecting.done():
                connecting.cancel()
            outcome = (await asyncio.gather(connecting, return_exceptions=True))[0]
            if not isinstance(outcome, BaseException):
                pending_connection = outcome
            if self._connect_task is connecting:
                self._connect_task = None

        receiver = self._receiver
        self._receiver = None
        if receiver is not None and not receiver.done():
            receiver.cancel()
        connection = self._connection
        self._connection = None
        connections = [connection] if connection is not None else []
        if pending_connection is not None and pending_connection is not connection:
            connections.append(pending_connection)
        close_results = await asyncio.gather(
            *(item.close() for item in connections),
            return_exceptions=True,
        )
        if receiver is not None:
            await asyncio.gather(receiver, return_exceptions=True)
        self._closed_event.set()
        errors = [result for result in close_results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("VTS connection close failed", errors)
