"""Minimal correlated client for the VTube Studio public WebSocket API."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast
from uuid import uuid4

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.provider_transport import (
    EndpointKind,
    build_ssl_context,
    validate_endpoint,
    validate_proxy_url,
)

VTS_API_NAME = "VTubeStudioPublicAPI"
VTS_API_VERSION = "1.0"
_WEBSOCKET_LOGGER = logging.getLogger("megumin.vts.websocket.transport")
_WEBSOCKET_LOGGER.setLevel(logging.WARNING)
_MAX_SERVER_MESSAGE_BYTES = 1024 * 1024
_MAX_SERVER_COLLECTION_ITEMS = 2048
_MAX_SERVER_DEPTH = 8
_MAX_SERVER_STRING_CHARS = 4096
_MAX_PARAMETER_COUNT = 1024
_MAX_INJECTED_PARAMETERS = 32
_MAX_VTS_NAME_CHARS = 256
_EVENT_QUEUE_CAPACITY = 64
_SUPPORTED_EVENTS = frozenset({"HotkeyTriggeredEvent", "ModelLoadedEvent"})
_EVENT_CLOSED = object()


def _reject_redirect(exc: Exception) -> Exception:
    return exc


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


class VTSAuthenticationError(VTSError):
    code = "vts_auth_failed"


class VTSConfigurationError(VTSError):
    """A stable preflight failure that must not enter a reconnect loop."""

    _ALLOWED_CODES = frozenset(
        {
            "vts_api_unavailable",
            "vts_auth_revoked",
            "vts_model_missing",
            "vts_hotkey_missing",
            "vts_hotkey_ambiguous",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._ALLOWED_CODES:
            raise ValueError("unsupported VTS configuration error code")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VTSPreflight:
    """Content-free capability summary safe for health and diagnostic snapshots."""

    available: bool
    authenticated: bool
    model_loaded: bool
    configured_hotkey_count: int
    missing_hotkey_count: int
    error_code: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.available
            and self.authenticated
            and self.model_loaded
            and self.missing_hotkey_count == 0
            and self.error_code is None
        )


@dataclass(frozen=True, slots=True)
class VTSParameterCapability:
    """One bounded VTS input parameter capability."""

    minimum: float
    maximum: float
    default: float


@dataclass(frozen=True, slots=True)
class VTSHotkeyTriggeredEvent:
    """Content-minimal hotkey event; model and private hotkey IDs are discarded."""

    hotkey_name: str
    triggered_by_api: bool


@dataclass(frozen=True, slots=True)
class VTSModelLoadedEvent:
    """Model lifecycle event without model identity or path."""

    model_loaded: bool


VTSEvent: TypeAlias = VTSHotkeyTriggeredEvent | VTSModelLoadedEvent


@dataclass(frozen=True, slots=True)
class _VTSHotkey:
    hotkey_id: str
    name: str | None


def _reject_non_finite(_value: str) -> None:
    raise VTSProtocolError


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VTSProtocolError
        result[key] = value
    return result


def _validate_server_value(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_SERVER_DEPTH:
        raise VTSProtocolError
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > 2**53:
            raise VTSProtocolError
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise VTSProtocolError
        return
    if isinstance(value, str):
        if (
            len(value) > _MAX_SERVER_STRING_CHARS
            or "\x00" in value
            or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        ):
            raise VTSProtocolError
        return
    if isinstance(value, list):
        if len(value) > _MAX_SERVER_COLLECTION_ITEMS:
            raise VTSProtocolError
        for item in value:
            _validate_server_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > _MAX_SERVER_COLLECTION_ITEMS:
            raise VTSProtocolError
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > _MAX_SERVER_STRING_CHARS:
                raise VTSProtocolError
            _validate_server_value(item, depth=depth + 1)
        return
    raise VTSProtocolError


def _decode_server_message(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > _MAX_SERVER_MESSAGE_BYTES:
        raise VTSProtocolError
    try:
        payload = json.loads(
            raw,
            parse_constant=_reject_non_finite,
            object_pairs_hook=_unique_object_pairs,
        )
    except VTSProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise VTSProtocolError from exc
    if not isinstance(payload, dict):
        raise VTSProtocolError
    _validate_server_value(payload)
    return payload


def _require_vts_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_VTS_NAME_CHARS
        or "\x00" in value
    ):
        raise VTSProtocolError
    return value


def _require_finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise VTSProtocolError
    return float(value)


def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise VTSProtocolError
    return value


def _require_local_name(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_VTS_NAME_CHARS
        or "\x00" in value
    ):
        raise ValueError(f"VTS {field} invalid")
    return value.strip()


def _require_expression_file(value: object) -> str:
    selected = _require_local_name(value, field="expression file")
    if (
        not selected.casefold().endswith(".exp3.json")
        or "/" in selected
        or "\\" in selected
        or selected in {".", ".."}
    ):
        raise ValueError("VTS expression file invalid")
    return selected


async def _default_connection_factory(
    uri: str,
    *,
    proxy_url: str | None,
    ca_bundle_path: Path | None,
) -> WebSocketConnection:
    policy = validate_endpoint(uri, kind=EndpointKind.websocket)
    options: dict[str, Any] = {"proxy": proxy_url}
    if policy.tls:
        context = build_ssl_context(ca_bundle_path)
        options["ssl"] = context
        if proxy_url is not None and proxy_url.startswith("https://"):
            options["proxy_ssl"] = context
    connector = websocket_connect(
        uri,
        open_timeout=5,
        close_timeout=3,
        ping_interval=20,
        ping_timeout=10,
        max_size=1024 * 1024,
        max_queue=16,
        logger=_WEBSOCKET_LOGGER,
        **options,
    )
    if hasattr(connector, "process_redirect"):
        cast(Any, connector).process_redirect = _reject_redirect
    connection = await connector
    return cast(WebSocketConnection, connection)


class VTSClient:
    def __init__(
        self,
        uri: str = "ws://127.0.0.1:8001",
        *,
        request_timeout_seconds: float = 5.0,
        proxy_url: str | None = None,
        ca_bundle_path: Path | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if request_timeout_seconds <= 0.0:
            raise ValueError("request_timeout_seconds 必须大于 0")
        validate_endpoint(uri, kind=EndpointKind.websocket)
        if proxy_url is not None:
            validate_proxy_url(proxy_url)
        self._uri = uri
        self._request_timeout = request_timeout_seconds
        self._proxy_url = proxy_url
        self._ca_bundle_path = ca_bundle_path
        self._connection_factory = connection_factory
        self._connection: WebSocketConnection | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[WebSocketConnection] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._pending: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._send_lock = asyncio.Lock()
        self._events: asyncio.Queue[VTSEvent | object] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_CAPACITY
        )
        self._dropped_event_count = 0
        self._closed_event = asyncio.Event()
        self._closed_event.set()
        self._closed = False

    @property
    def connected(self) -> bool:
        return not self._closed and self._connection is not None and not self._closed_event.is_set()

    @property
    def dropped_event_count(self) -> int:
        return self._dropped_event_count

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
        except (OSError, TimeoutError, ConnectionClosed, WebSocketException) as exc:
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
        self._reset_event_queue()
        self._closed_event.clear()
        self._receiver = asyncio.create_task(self._receive_loop(), name="vts-receiver")

    async def _open_connection(self) -> WebSocketConnection:
        if self._connection_factory is not None:
            return await self._connection_factory(self._uri)
        return await _default_connection_factory(
            self._uri,
            proxy_url=self._proxy_url,
            ca_bundle_path=self._ca_bundle_path,
        )

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

    async def current_model(self) -> dict[str, Any]:
        return await self.request("CurrentModelRequest")

    async def current_model_name(self) -> str:
        """Return the current private model name for in-memory config selection only."""

        data = await self.current_model()
        model_loaded = data.get("modelLoaded")
        if not isinstance(model_loaded, bool):
            raise VTSProtocolError
        if not model_loaded:
            raise VTSConfigurationError("vts_model_missing")
        return _require_vts_name(data.get("modelName"))

    async def available_hotkey_ids(self) -> frozenset[str]:
        return frozenset(item.hotkey_id for item in await self._available_hotkeys())

    async def resolve_hotkey_name(self, name: str) -> str:
        """Resolve one current-model hotkey by a unique normalized name.

        VTube Studio preserves accidental boundary whitespace in user-entered
        hotkey names.  Local configuration remains canonical while the
        untrusted VTS boundary is stripped and compared case-insensitively.
        Multiple names that normalize to the same value still fail closed.
        """

        selected_name = _require_local_name(name, field="hotkey name")
        matches = [
            item
            for item in await self._available_hotkeys()
            if item.name is not None and item.name.strip().casefold() == selected_name.casefold()
        ]
        if not matches:
            raise VTSConfigurationError("vts_hotkey_missing")
        if len(matches) != 1:
            raise VTSConfigurationError("vts_hotkey_ambiguous")
        return matches[0].hotkey_id

    async def _available_hotkeys(self) -> tuple[_VTSHotkey, ...]:
        data = await self.request("HotkeysInCurrentModelRequest")
        values = data.get("availableHotkeys")
        if not isinstance(values, list) or len(values) > _MAX_PARAMETER_COUNT:
            raise VTSProtocolError
        hotkeys: list[_VTSHotkey] = []
        for value in values:
            if not isinstance(value, dict):
                raise VTSProtocolError
            raw_name = value.get("name")
            hotkeys.append(
                _VTSHotkey(
                    hotkey_id=_require_vts_name(value.get("hotkeyID")),
                    name=None if raw_name is None else _require_vts_name(raw_name),
                )
            )
        return tuple(hotkeys)

    async def parameter_capabilities(self) -> dict[str, VTSParameterCapability]:
        """Return current VTS input parameters used by parameter injection."""

        data = await self.request("InputParameterListRequest")
        capabilities: dict[str, VTSParameterCapability] = {}
        total = 0
        for collection_name in ("defaultParameters", "customParameters"):
            values = data.get(collection_name)
            if not isinstance(values, list):
                raise VTSProtocolError
            total += len(values)
            if total > _MAX_PARAMETER_COUNT:
                raise VTSProtocolError
            for value in values:
                if not isinstance(value, dict):
                    raise VTSProtocolError
                parameter_id = _require_vts_name(value.get("name"))
                minimum = _require_finite_number(value.get("min"))
                maximum = _require_finite_number(value.get("max"))
                default = _require_finite_number(value.get("defaultValue"))
                if minimum > maximum or not minimum <= default <= maximum:
                    raise VTSProtocolError
                if parameter_id in capabilities:
                    raise VTSProtocolError
                capabilities[parameter_id] = VTSParameterCapability(
                    minimum=minimum,
                    maximum=maximum,
                    default=default,
                )
        return capabilities

    async def inject_parameter_values(
        self,
        values: Mapping[str, float],
        *,
        face_found: bool,
    ) -> None:
        if not values or len(values) > _MAX_INJECTED_PARAMETERS:
            raise ValueError("VTS parameter frame size invalid")
        parameter_values: list[dict[str, float | str]] = []
        for parameter_id, raw_value in values.items():
            selected_id = _require_local_name(parameter_id, field="parameter id")
            if (
                isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
                or not math.isfinite(raw_value)
                or abs(float(raw_value)) > 1_000_000
            ):
                raise ValueError("VTS parameter value invalid")
            parameter_values.append({"id": selected_id, "value": float(raw_value), "weight": 1.0})
        await self.request(
            "InjectParameterDataRequest",
            {
                "faceFound": bool(face_found),
                "mode": "set",
                "parameterValues": parameter_values,
            },
        )

    async def expression_is_active(self, expression_file: str) -> bool:
        selected_file = _require_expression_file(expression_file)
        data = await self.request("ExpressionStateRequest")
        values = data.get("expressions")
        if not isinstance(values, list) or len(values) > _MAX_PARAMETER_COUNT:
            raise VTSProtocolError
        matches: list[bool] = []
        for value in values:
            if not isinstance(value, dict):
                raise VTSProtocolError
            file_name = value.get("file")
            active = value.get("active")
            if not isinstance(file_name, str) or not isinstance(active, bool):
                raise VTSProtocolError
            if file_name.casefold() == selected_file.casefold():
                matches.append(active)
        if len(matches) != 1:
            raise VTSConfigurationError(
                "vts_hotkey_missing" if not matches else "vts_hotkey_ambiguous"
            )
        return matches[0]

    async def set_expression_active(
        self,
        expression_file: str,
        *,
        active: bool,
        fade_seconds: float,
    ) -> None:
        selected_file = _require_expression_file(expression_file)
        if (
            not isinstance(active, bool)
            or isinstance(fade_seconds, bool)
            or not isinstance(fade_seconds, (int, float))
            or not math.isfinite(fade_seconds)
            or not 0.0 <= float(fade_seconds) <= 2.0
        ):
            raise ValueError("VTS expression activation invalid")
        await self.request(
            "ExpressionActivationRequest",
            {
                "expressionFile": selected_file,
                "active": active,
                "fadeTime": float(fade_seconds),
            },
        )

    async def subscribe_event(self, event_name: str, *, subscribe: bool = True) -> None:
        if event_name not in _SUPPORTED_EVENTS or not isinstance(subscribe, bool):
            raise ValueError("unsupported VTS event subscription")
        data = await self.request(
            "EventSubscriptionRequest",
            {"eventName": event_name, "subscribe": subscribe, "config": {}},
        )
        subscribed_event_count = data.get("subscribedEventCount")
        subscribed_events = data.get("subscribedEvents")
        if (
            isinstance(subscribed_event_count, bool)
            or not isinstance(subscribed_event_count, int)
            or not 0 <= subscribed_event_count <= _MAX_SERVER_COLLECTION_ITEMS
            or not isinstance(subscribed_events, list)
            or len(subscribed_events) != subscribed_event_count
        ):
            raise VTSProtocolError
        event_names: set[str] = set()
        for raw_event_name in subscribed_events:
            subscribed_event_name = _require_vts_name(raw_event_name)
            if subscribed_event_name in event_names:
                raise VTSProtocolError
            event_names.add(subscribed_event_name)
        if (event_name in event_names) is not subscribe:
            raise VTSProtocolError

    async def next_event(self) -> VTSEvent:
        event = await self._events.get()
        if event is _EVENT_CLOSED:
            self._offer_event(_EVENT_CLOSED)
            raise VTSConnectionError
        return cast(VTSEvent, event)

    async def preflight(self, required_hotkey_ids: frozenset[str]) -> VTSPreflight:
        """Verify API/auth/model/hotkeys without returning names, IDs, or paths."""

        state = await self.api_state()
        active = state.get("active")
        authenticated = state.get("currentSessionAuthenticated")
        if not isinstance(active, bool) or not isinstance(authenticated, bool):
            raise VTSProtocolError
        if not active:
            return VTSPreflight(
                available=False,
                authenticated=authenticated,
                model_loaded=False,
                configured_hotkey_count=len(required_hotkey_ids),
                missing_hotkey_count=len(required_hotkey_ids),
                error_code="vts_api_unavailable",
            )
        if not authenticated:
            return VTSPreflight(
                available=True,
                authenticated=False,
                model_loaded=False,
                configured_hotkey_count=len(required_hotkey_ids),
                missing_hotkey_count=len(required_hotkey_ids),
                error_code="vts_auth_revoked",
            )

        model = await self.current_model()
        model_loaded = model.get("modelLoaded")
        if not isinstance(model_loaded, bool):
            raise VTSProtocolError
        if not model_loaded:
            return VTSPreflight(
                available=True,
                authenticated=True,
                model_loaded=False,
                configured_hotkey_count=len(required_hotkey_ids),
                missing_hotkey_count=len(required_hotkey_ids),
                error_code="vts_model_missing",
            )

        available = await self.available_hotkey_ids()
        missing_count = len(required_hotkey_ids - available)
        return VTSPreflight(
            available=True,
            authenticated=True,
            model_loaded=True,
            configured_hotkey_count=len(required_hotkey_ids),
            missing_hotkey_count=missing_count,
            error_code="vts_hotkey_missing" if missing_count else None,
        )

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
        except VTSProtocolError as exc:
            failure = exc
        except (UnicodeError, json.JSONDecodeError):
            failure = VTSProtocolError()
        except (OSError, ConnectionClosed):
            pass
        finally:
            if self._connection is connection:
                self._connection = None
            self._closed_event.set()
            self._offer_event(_EVENT_CLOSED)
            for _response_type, future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(failure)

    def _dispatch(self, raw: str) -> None:
        payload = _decode_server_message(raw)
        message_type = payload.get("messageType")
        if message_type in _SUPPORTED_EVENTS:
            request_id = payload.get("requestID")
            if "requestID" in payload and (
                not isinstance(request_id, str)
                or not request_id
                or len(request_id) > _MAX_VTS_NAME_CHARS
            ):
                raise VTSProtocolError
            self._dispatch_event(payload)
            return
        request_id = payload.get("requestID")
        if request_id is None:
            self._dispatch_event(payload)
            return
        if not isinstance(request_id, str):
            raise VTSProtocolError
        pending = self._pending.get(request_id)
        if pending is None:
            return
        response_type, future = pending
        if future.done():
            return
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

    def _dispatch_event(self, payload: dict[str, Any]) -> None:
        message_type = payload.get("messageType")
        if message_type not in _SUPPORTED_EVENTS:
            return
        if payload.get("apiName") != VTS_API_NAME or payload.get("apiVersion") != VTS_API_VERSION:
            raise VTSProtocolError
        data = payload.get("data")
        if not isinstance(data, dict):
            raise VTSProtocolError
        if message_type == "HotkeyTriggeredEvent":
            event: VTSEvent = VTSHotkeyTriggeredEvent(
                hotkey_name=_require_vts_name(data.get("hotkeyName")),
                triggered_by_api=_require_bool(data.get("hotkeyTriggeredByAPI")),
            )
        else:
            event = VTSModelLoadedEvent(
                model_loaded=_require_bool(data.get("modelLoaded")),
            )
        self._offer_event(event)

    def _offer_event(self, event: VTSEvent | object) -> None:
        if self._events.full():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                self._dropped_event_count += 1
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_event_count += 1

    def _reset_event_queue(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._dropped_event_count = 0

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
        self._offer_event(_EVENT_CLOSED)
        errors = [result for result in close_results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("VTS connection close failed", errors)
