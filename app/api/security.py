"""Fail-closed security boundary for the explicitly enabled development API."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config.logging import log_event
from app.limits import LimitsConfig

DEV_API_PROTOCOL_VERSION = 1
DEV_API_MAX_BODY_BYTES = 64 * 1024
DEV_API_MAX_FRAME_BYTES = 64 * 1024
DEV_API_TOKEN_TTL_SECONDS = 60 * 60

_SESSION_ID_PATTERN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
_SECURITY_RESPONSE_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-megumin-protocol", str(DEV_API_PROTOCOL_VERSION).encode("ascii")),
)


class DevAPIScope(StrEnum):
    """Scopes granted to one process-local development credential."""

    chat = "chat"
    admin = "admin"


class DevAPISecurityError(RuntimeError):
    """A stable, body-free security rejection suitable for HTTP or WebSocket."""

    def __init__(
        self,
        code: str,
        *,
        reason: str,
        http_status: int,
        websocket_code: int = 1008,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.http_status = http_status
        self.websocket_code = websocket_code
        self.retry_after_seconds = retry_after_seconds


def _format_authority(host: str, port: int | None) -> str:
    formatted_host = f"[{host}]" if ":" in host else host
    return f"{formatted_host}:{port}" if port is not None else formatted_host


def canonical_authority(value: str) -> str:
    """Canonicalize one Host-style authority and reject ambiguous syntax."""

    candidate = value.strip()
    if (
        not candidate
        or any(character in candidate for character in "\\/?#")
        or any(character.isspace() for character in candidate)
    ):
        raise ValueError("host authority is invalid")
    try:
        parsed = urlsplit(f"//{candidate}", allow_fragments=False)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("host authority is invalid") from exc
    if (
        host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("host authority is invalid")
    return _format_authority(host.lower(), port)


def canonical_origin(value: str) -> str:
    """Canonicalize one exact HTTP origin; wildcards and opaque origins are forbidden."""

    candidate = value.strip()
    if (
        not candidate
        or candidate == "*"
        or candidate.lower() == "null"
        or any(character.isspace() for character in candidate)
    ):
        raise ValueError("origin must be one explicit HTTP origin")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin is invalid") from exc
    if (
        parsed.scheme.lower() != "http"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin must be one explicit HTTP origin without path or query")
    return f"http://{_format_authority(parsed.hostname.lower(), None if port == 80 else port)}"


def validate_loopback_host(host: str) -> str:
    """Return a canonical numeric loopback address or fail closed."""

    candidate = host.strip()
    if candidate.startswith("[") != candidate.endswith("]"):
        raise ValueError("development API host must be a numeric loopback address")
    if candidate.startswith("["):
        candidate = candidate[1:-1]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError("development API host must be a numeric loopback address") from exc
    if not address.is_loopback:
        raise ValueError("development API host must be a numeric loopback address")
    return address.compressed


@dataclass(frozen=True, slots=True)
class DevAPIConfig:
    """Immutable process-local credential and hard transport limits."""

    token: str = field(repr=False)
    client_id: str
    session_id: str
    allowed_origins: frozenset[str]
    allowed_hosts: frozenset[str]
    scopes: frozenset[DevAPIScope] = frozenset({DevAPIScope.chat})
    protocol_version: int = DEV_API_PROTOCOL_VERSION
    token_ttl_seconds: float = DEV_API_TOKEN_TTL_SECONDS
    max_http_body_bytes: int = DEV_API_MAX_BODY_BYTES
    max_websocket_frame_bytes: int = DEV_API_MAX_FRAME_BYTES
    max_json_depth: int = 16
    max_metadata_bytes: int = 8 * 1024
    max_metadata_depth: int = 3
    max_metadata_keys: int = 16
    max_metadata_nodes: int = 128
    max_client_clock_skew_seconds: float = 5 * 60
    http_rate_limit: int = 120
    http_rate_window_seconds: float = 60.0
    http_concurrency_limit: int = 8
    websocket_message_rate_limit: int = 120
    websocket_message_rate_window_seconds: float = 60.0
    websocket_connection_limit: int = 2
    security_log_rate_limit: int = 10
    security_log_window_seconds: float = 60.0
    created_monotonic: float = field(default_factory=time.monotonic, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.token) < 32 or any(character.isspace() for character in self.token):
            raise ValueError("development API token must contain at least 32 non-space characters")
        for label, value in (("client", self.client_id), ("session", self.session_id)):
            if not 1 <= len(value) <= 128 or any(
                character not in _SESSION_ID_PATTERN for character in value
            ):
                raise ValueError(f"development API {label} id is invalid")
        if self.protocol_version != DEV_API_PROTOCOL_VERSION:
            raise ValueError("unsupported development API protocol version")
        if not self.allowed_origins or not self.allowed_hosts or not self.scopes:
            raise ValueError("development API origins, hosts, and scopes cannot be empty")
        canonical_origins = frozenset(canonical_origin(value) for value in self.allowed_origins)
        canonical_hosts = frozenset(canonical_authority(value) for value in self.allowed_hosts)
        for authority in canonical_hosts:
            authority_host = urlsplit(f"//{authority}", allow_fragments=False).hostname
            if authority_host is None:
                raise ValueError("development API host authority is invalid")
            validate_loopback_host(authority_host)
        object.__setattr__(self, "allowed_origins", canonical_origins)
        object.__setattr__(self, "allowed_hosts", canonical_hosts)
        if self.token_ttl_seconds != DEV_API_TOKEN_TTL_SECONDS:
            raise ValueError("development API token TTL is fixed at one hour")
        if (
            self.max_http_body_bytes > DEV_API_MAX_BODY_BYTES
            or self.max_websocket_frame_bytes > DEV_API_MAX_FRAME_BYTES
        ):
            raise ValueError("development API body and frame limits cannot exceed 64 KiB")
        positive_values = (
            self.token_ttl_seconds,
            self.max_http_body_bytes,
            self.max_websocket_frame_bytes,
            self.max_json_depth,
            self.max_metadata_bytes,
            self.max_metadata_depth,
            self.max_metadata_keys,
            self.max_metadata_nodes,
            self.max_client_clock_skew_seconds,
            self.http_rate_limit,
            self.http_rate_window_seconds,
            self.http_concurrency_limit,
            self.websocket_message_rate_limit,
            self.websocket_message_rate_window_seconds,
            self.websocket_connection_limit,
            self.security_log_rate_limit,
            self.security_log_window_seconds,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("development API limits must be positive")

    @classmethod
    def generate(
        cls,
        *,
        host: str,
        port: int,
        origins: Iterable[str] = (),
        scopes: Iterable[DevAPIScope] = (DevAPIScope.chat,),
    ) -> DevAPIConfig:
        """Generate a new credential bound to one loopback listener and one session."""

        canonical_host = validate_loopback_host(host)
        if not 1 <= port <= 65_535:
            raise ValueError("development API port must be between 1 and 65535")
        authority = _format_authority(canonical_host, port)
        selected_origins = tuple(origins) or (f"http://{authority}",)
        allowed_hosts = {authority}
        if port == 80:
            allowed_hosts.add(_format_authority(canonical_host, None))
        return cls(
            token=secrets.token_urlsafe(32),
            client_id=f"client_{secrets.token_hex(16)}",
            session_id=f"session_{secrets.token_hex(16)}",
            allowed_origins=frozenset(selected_origins),
            allowed_hosts=frozenset(allowed_hosts),
            scopes=frozenset(scopes),
        )

    def client_headers(self) -> dict[str, str]:
        """Build the explicit headers required by a trusted development client."""

        return {
            "Authorization": f"Bearer {self.token}",
            "Origin": sorted(self.allowed_origins)[0],
            "X-Megumin-Protocol": str(self.protocol_version),
            "X-Megumin-Client-ID": self.client_id,
            "X-Megumin-Session-ID": self.session_id,
        }


def apply_hard_limits(config: DevAPIConfig, limits: LimitsConfig) -> DevAPIConfig:
    """Clamp a generated credential to the centralized W07 transport caps."""

    return replace(
        config,
        max_http_body_bytes=min(config.max_http_body_bytes, limits.dev_http_body_bytes),
        max_websocket_frame_bytes=min(
            config.max_websocket_frame_bytes,
            limits.dev_websocket_frame_bytes,
        ),
        max_metadata_bytes=min(config.max_metadata_bytes, limits.metadata_bytes),
        max_metadata_depth=min(config.max_metadata_depth, limits.metadata_depth),
        max_metadata_keys=min(config.max_metadata_keys, limits.metadata_keys),
        max_metadata_nodes=min(config.max_metadata_nodes, limits.metadata_nodes),
    )


@dataclass(frozen=True, slots=True)
class DevAPIPrincipal:
    """Authenticated identity attached to one ASGI request scope."""

    client_id: str
    session_id: str
    scopes: frozenset[DevAPIScope]
    legacy_protocol: bool = False

    @property
    def session_fingerprint(self) -> str:
        return hashlib.sha256(self.session_id.encode("ascii")).hexdigest()[:12]

    @property
    def client_fingerprint(self) -> str:
        return hashlib.sha256(self.client_id.encode("ascii")).hexdigest()[:12]


def _header_values(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> list[str]:
    lowered = name.lower()
    return [value.decode("latin-1") for key, value in headers if key.lower() == lowered]


def _single_header(
    headers: Sequence[tuple[bytes, bytes]],
    name: bytes,
    *,
    missing_reason: str,
    duplicate_reason: str,
) -> str:
    values = _header_values(headers, name)
    if not values:
        raise DevAPISecurityError(
            "authentication_failed" if name == b"authorization" else "security_headers_invalid",
            reason=missing_reason,
            http_status=401 if name == b"authorization" else 400,
        )
    if len(values) != 1:
        raise DevAPISecurityError(
            "authentication_failed" if name == b"authorization" else "security_headers_invalid",
            reason=duplicate_reason,
            http_status=401 if name == b"authorization" else 400,
        )
    return values[0]


class DevAPISecurity:
    """Bounded authentication, rate, and connection state for one app instance."""

    def __init__(
        self,
        config: DevAPIConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._http_timestamps: deque[float] = deque()
        self._websocket_timestamps: deque[float] = deque()
        self._security_log_timestamps: deque[float] = deque()
        self._active_http = 0
        self._active_websockets = 0
        self._logger: logging.Logger | None = None

    def set_logger(self, logger: logging.Logger) -> None:
        self._logger = logger

    def log_rejection(
        self,
        error: DevAPISecurityError,
        *,
        transport: str,
        principal: DevAPIPrincipal | None = None,
    ) -> None:
        if self._logger is None:
            return
        now = self._clock()
        with self._lock:
            self._discard_expired(
                self._security_log_timestamps,
                now=now,
                window=self.config.security_log_window_seconds,
            )
            if len(self._security_log_timestamps) >= self.config.security_log_rate_limit:
                return
            self._security_log_timestamps.append(now)
        fields: dict[str, Any] = {
            "reason": error.reason,
            "transport": transport,
        }
        if principal is not None:
            fields["client_fingerprint"] = principal.client_fingerprint
            fields["session_fingerprint"] = principal.session_fingerprint
        log_event(self._logger, logging.WARNING, "dev_api.request_rejected", **fields)

    def authorize(self, headers: Sequence[tuple[bytes, bytes]]) -> DevAPIPrincipal:
        authorization = _single_header(
            headers,
            b"authorization",
            missing_reason="authorization_missing",
            duplicate_reason="authorization_duplicated",
        )
        scheme, separator, candidate = authorization.partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not candidate
            or candidate != candidate.strip()
            or not secrets.compare_digest(
                candidate.encode("utf-8"),
                self.config.token.encode("utf-8"),
            )
        ):
            raise DevAPISecurityError(
                "authentication_failed",
                reason="authorization_rejected",
                http_status=401,
            )
        self.ensure_token_fresh()

        protocol = _single_header(
            headers,
            b"x-megumin-protocol",
            missing_reason="protocol_missing",
            duplicate_reason="protocol_duplicated",
        )
        if protocol != str(self.config.protocol_version):
            raise DevAPISecurityError(
                "protocol_version_unsupported",
                reason="protocol_rejected",
                http_status=426,
                websocket_code=1002,
            )

        origin = _single_header(
            headers,
            b"origin",
            missing_reason="origin_missing",
            duplicate_reason="origin_duplicated",
        )
        try:
            normalized_origin = canonical_origin(origin)
        except ValueError as exc:
            raise DevAPISecurityError(
                "origin_forbidden",
                reason="origin_invalid",
                http_status=403,
            ) from exc
        if normalized_origin not in self.config.allowed_origins:
            raise DevAPISecurityError(
                "origin_forbidden",
                reason="origin_not_allowed",
                http_status=403,
            )

        host = _single_header(
            headers,
            b"host",
            missing_reason="host_missing",
            duplicate_reason="host_duplicated",
        )
        try:
            normalized_host = canonical_authority(host)
        except ValueError as exc:
            raise DevAPISecurityError(
                "host_forbidden",
                reason="host_invalid",
                http_status=400,
            ) from exc
        if normalized_host not in self.config.allowed_hosts:
            raise DevAPISecurityError(
                "host_forbidden",
                reason="host_not_allowed",
                http_status=403,
            )

        client_values = _header_values(headers, b"x-megumin-client-id")
        if len(client_values) > 1:
            raise DevAPISecurityError(
                "client_identity_duplicated",
                reason="client_identity_duplicated",
                http_status=400,
            )
        legacy_protocol = not client_values
        if client_values and not secrets.compare_digest(
            client_values[0].encode("utf-8"),
            self.config.client_id.encode("utf-8"),
        ):
            raise DevAPISecurityError(
                "client_identity_forbidden",
                reason="client_identity_not_allowed",
                http_status=403,
            )

        session_id = _single_header(
            headers,
            b"x-megumin-session-id",
            missing_reason="session_missing",
            duplicate_reason="session_duplicated",
        )
        if not secrets.compare_digest(
            session_id.encode("utf-8"),
            self.config.session_id.encode("utf-8"),
        ):
            raise DevAPISecurityError(
                "session_forbidden",
                reason="session_not_allowed",
                http_status=403,
            )
        return DevAPIPrincipal(
            client_id=self.config.client_id,
            session_id=self.config.session_id,
            scopes=self.config.scopes,
            legacy_protocol=legacy_protocol,
        )

    def token_seconds_remaining(self) -> float:
        """Return the bounded lifetime left for this process-local credential."""

        elapsed = max(0.0, self._clock() - self.config.created_monotonic)
        return max(0.0, self.config.token_ttl_seconds - elapsed)

    def ensure_token_fresh(self) -> None:
        """Reject both new requests and established transports after token expiry."""

        if self.token_seconds_remaining() <= 0:
            raise DevAPISecurityError(
                "authentication_failed",
                reason="token_expired",
                http_status=401,
            )

    def require_scope(
        self,
        principal: DevAPIPrincipal,
        required: DevAPIScope,
    ) -> None:
        if required not in principal.scopes:
            raise DevAPISecurityError(
                "scope_forbidden",
                reason=f"scope_{required.value}_missing",
                http_status=403,
            )

    @staticmethod
    def _discard_expired(timestamps: deque[float], *, now: float, window: float) -> None:
        cutoff = now - window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    def begin_http_request(self) -> None:
        now = self._clock()
        with self._lock:
            self._discard_expired(
                self._http_timestamps,
                now=now,
                window=self.config.http_rate_window_seconds,
            )
            if len(self._http_timestamps) >= self.config.http_rate_limit:
                raise DevAPISecurityError(
                    "rate_limit_exceeded",
                    reason="http_rate_limit",
                    http_status=429,
                    websocket_code=1013,
                    retry_after_seconds=math.ceil(self.config.http_rate_window_seconds),
                )
            if self._active_http >= self.config.http_concurrency_limit:
                raise DevAPISecurityError(
                    "connection_limit_exceeded",
                    reason="http_concurrency_limit",
                    http_status=429,
                    websocket_code=1013,
                    retry_after_seconds=1,
                )
            self._http_timestamps.append(now)
            self._active_http += 1

    def end_http_request(self) -> None:
        with self._lock:
            self._active_http = max(0, self._active_http - 1)

    def begin_websocket(self) -> None:
        with self._lock:
            if self._active_websockets >= self.config.websocket_connection_limit:
                raise DevAPISecurityError(
                    "connection_limit_exceeded",
                    reason="websocket_connection_limit",
                    http_status=429,
                    websocket_code=1013,
                    retry_after_seconds=1,
                )
            self._active_websockets += 1

    def end_websocket(self) -> None:
        with self._lock:
            self._active_websockets = max(0, self._active_websockets - 1)

    def consume_websocket_message(self) -> None:
        self.ensure_token_fresh()
        now = self._clock()
        with self._lock:
            self._discard_expired(
                self._websocket_timestamps,
                now=now,
                window=self.config.websocket_message_rate_window_seconds,
            )
            if len(self._websocket_timestamps) >= self.config.websocket_message_rate_limit:
                raise DevAPISecurityError(
                    "rate_limit_exceeded",
                    reason="websocket_message_rate_limit",
                    http_status=429,
                    websocket_code=1013,
                    retry_after_seconds=math.ceil(
                        self.config.websocket_message_rate_window_seconds
                    ),
                )
            self._websocket_timestamps.append(now)


def json_nesting_exceeds(data: bytes, *, maximum: int) -> bool:
    """Bound JSON nesting before invoking a JSON parser, ignoring braces inside strings."""

    depth = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in {0x5B, 0x7B}:  # [ or {
            depth += 1
            if depth > maximum:
                return True
        elif byte in {0x5D, 0x7D}:  # ] or }
            depth = max(0, depth - 1)
    return False


async def _send_http_error(send: Send, error: DevAPISecurityError) -> None:
    body = json.dumps({"detail": error.code}, separators=(",", ":")).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        *_SECURITY_RESPONSE_HEADERS,
    ]
    if error.http_status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    if error.retry_after_seconds is not None:
        headers.append((b"retry-after", str(error.retry_after_seconds).encode("ascii")))
    await send(
        {
            "type": "http.response.start",
            "status": error.http_status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


class DevAPIGuardMiddleware:
    """Authenticate HTTP/WS and bound HTTP bytes before FastAPI parses JSON."""

    def __init__(self, app: ASGIApp, *, security: DevAPISecurity | None) -> None:
        self._app = app
        self._security = security

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        if self._security is None:
            error = DevAPISecurityError(
                "dev_api_unavailable",
                reason="dev_api_not_explicitly_enabled",
                http_status=503,
                websocket_code=1008,
            )
            if scope_type == "http":
                await _send_http_error(send, error)
            else:
                await send(
                    {"type": "websocket.close", "code": error.websocket_code, "reason": error.code}
                )
            return

        headers = scope.get("headers", [])
        try:
            self._require_loopback_peer(scope)
            principal = self._security.authorize(headers)
        except DevAPISecurityError as error:
            self._security.log_rejection(error, transport=scope_type)
            if scope_type == "http":
                await _send_http_error(send, error)
            else:
                await send(
                    {"type": "websocket.close", "code": error.websocket_code, "reason": error.code}
                )
            return

        state = scope.setdefault("state", {})
        state["dev_api_principal"] = principal
        state["dev_api_security"] = self._security
        if scope_type == "websocket":
            await self._app(scope, receive, send)
            return
        await self._guard_http(scope, receive, send, principal)

    @staticmethod
    def _require_loopback_peer(scope: Scope) -> None:
        client = scope.get("client")
        if not isinstance(client, tuple) or not client:
            raise DevAPISecurityError(
                "client_forbidden",
                reason="client_address_missing",
                http_status=403,
            )
        try:
            validate_loopback_host(str(client[0]))
        except ValueError as exc:
            raise DevAPISecurityError(
                "client_forbidden",
                reason="client_not_loopback",
                http_status=403,
            ) from exc

    async def _guard_http(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        principal: DevAPIPrincipal,
    ) -> None:
        assert self._security is not None
        admitted = False
        try:
            self._security.begin_http_request()
            admitted = True
            body = await self._read_bounded_body(scope, receive)
            content_types = _header_values(scope.get("headers", []), b"content-type")
            if len(content_types) > 1:
                raise DevAPISecurityError(
                    "content_type_invalid",
                    reason="content_type_duplicated",
                    http_status=400,
                )
            media_type = (
                content_types[0].split(";", maxsplit=1)[0].strip().lower() if content_types else ""
            )
            is_json = (
                not content_types
                or media_type == "application/json"
                or (media_type.startswith("application/") and media_type.endswith("+json"))
            )
            if (
                body
                and is_json
                and json_nesting_exceeds(body, maximum=self._security.config.max_json_depth)
            ):
                raise DevAPISecurityError(
                    "json_nesting_too_deep",
                    reason="json_depth_limit",
                    http_status=400,
                )

            replayed = False

            async def replay_receive() -> Message:
                nonlocal replayed
                if replayed:
                    return {"type": "http.disconnect"}
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def secure_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    secured_names = {name for name, _value in _SECURITY_RESPONSE_HEADERS}
                    response_headers = [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.lower() not in secured_names
                    ]
                    response_headers.extend(_SECURITY_RESPONSE_HEADERS)
                    message = {**message, "headers": response_headers}
                await send(message)

            await self._app(scope, replay_receive, secure_send)
        except DevAPISecurityError as error:
            self._security.log_rejection(error, transport="http", principal=principal)
            await _send_http_error(send, error)
        finally:
            if admitted:
                self._security.end_http_request()

    async def _read_bounded_body(self, scope: Scope, receive: Receive) -> bytes:
        assert self._security is not None
        headers = scope.get("headers", [])
        content_lengths = _header_values(headers, b"content-length")
        transfer_encodings = _header_values(headers, b"transfer-encoding")
        if len(transfer_encodings) > 1 or (content_lengths and transfer_encodings):
            raise DevAPISecurityError(
                "content_length_invalid",
                reason="ambiguous_body_framing",
                http_status=400,
            )
        if len(content_lengths) > 1:
            raise DevAPISecurityError(
                "content_length_invalid",
                reason="content_length_duplicated",
                http_status=400,
            )
        if content_lengths:
            try:
                declared = int(content_lengths[0])
            except ValueError as exc:
                raise DevAPISecurityError(
                    "content_length_invalid",
                    reason="content_length_malformed",
                    http_status=400,
                ) from exc
            if declared < 0:
                raise DevAPISecurityError(
                    "content_length_invalid",
                    reason="content_length_negative",
                    http_status=400,
                )
            if declared > self._security.config.max_http_body_bytes:
                raise DevAPISecurityError(
                    "request_body_too_large",
                    reason="content_length_limit",
                    http_status=413,
                )

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise DevAPISecurityError(
                    "request_disconnected",
                    reason="request_disconnected",
                    http_status=400,
                )
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self._security.config.max_http_body_bytes:
                raise DevAPISecurityError(
                    "request_body_too_large",
                    reason="streamed_body_limit",
                    http_status=413,
                )
            if not message.get("more_body", False):
                return bytes(body)


def principal_from_scope(scope: Scope) -> DevAPIPrincipal:
    state = scope.get("state", {})
    principal = state.get("dev_api_principal")
    if not isinstance(principal, DevAPIPrincipal):
        raise RuntimeError("development API principal is unavailable")
    return principal


def security_from_scope(scope: Scope) -> DevAPISecurity:
    state = scope.get("state", {})
    security = state.get("dev_api_security")
    if not isinstance(security, DevAPISecurity):
        raise RuntimeError("development API security state is unavailable")
    return security
