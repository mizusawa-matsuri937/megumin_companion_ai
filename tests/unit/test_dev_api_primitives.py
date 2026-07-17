"""W04 development API credential, protocol, and limiter invariants."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from app.api.protocol import (
    DevAPIProtocolError,
    ensure_authorized_session,
    parse_websocket_command,
    validate_metadata,
    validate_user_message,
)
from app.api.routes import _close_websocket_when_token_expires
from app.api.security import (
    DEV_API_MAX_BODY_BYTES,
    DEV_API_MAX_FRAME_BYTES,
    DEV_API_PROTOCOL_VERSION,
    DEV_API_TOKEN_TTL_SECONDS,
    DevAPIConfig,
    DevAPIGuardMiddleware,
    DevAPIPrincipal,
    DevAPIScope,
    DevAPISecurity,
    DevAPISecurityError,
    canonical_authority,
    canonical_origin,
    json_nesting_exceeds,
    validate_loopback_host,
)
from app.schemas import UserMessage
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocket

TOKEN = "w04-test-token-00000000000000000000000000000000"


def security_config(**updates: object) -> DevAPIConfig:
    base = DevAPIConfig(
        token=TOKEN,
        session_id="session_w04_test",
        allowed_origins=frozenset({"http://127.0.0.1:8765"}),
        allowed_hosts=frozenset({"127.0.0.1:8765"}),
        created_monotonic=100.0,
    )
    return replace(base, **updates)  # type: ignore[arg-type]


def raw_headers(config: DevAPIConfig, **updates: str | None) -> list[tuple[bytes, bytes]]:
    headers = {
        "authorization": f"Bearer {config.token}",
        "origin": sorted(config.allowed_origins)[0],
        "host": sorted(config.allowed_hosts)[0],
        "x-megumin-protocol": str(config.protocol_version),
        "x-megumin-session-id": config.session_id,
    }
    for key, value in updates.items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    return [(key.encode("ascii"), value.encode("latin-1")) for key, value in headers.items()]


def test_generated_credentials_are_random_loopback_bound_and_repr_safe() -> None:
    first = DevAPIConfig.generate(host="127.0.0.2", port=9011)
    second = DevAPIConfig.generate(host="127.0.0.2", port=9011)

    assert first.token != second.token
    assert first.session_id != second.session_id
    assert first.allowed_hosts == frozenset({"127.0.0.2:9011"})
    assert first.allowed_origins == frozenset({"http://127.0.0.2:9011"})
    assert first.token not in repr(first)
    assert first.client_headers()["Authorization"] == f"Bearer {first.token}"
    assert first.client_headers()["X-Megumin-Protocol"] == str(DEV_API_PROTOCOL_VERSION)


def test_security_hard_limits_and_host_authority_cannot_be_relaxed() -> None:
    with pytest.raises(ValueError, match="TTL"):
        security_config(token_ttl_seconds=DEV_API_TOKEN_TTL_SECONDS + 1)
    with pytest.raises(ValueError, match="64 KiB"):
        security_config(max_http_body_bytes=DEV_API_MAX_BODY_BYTES + 1)
    with pytest.raises(ValueError, match="64 KiB"):
        security_config(max_websocket_frame_bytes=DEV_API_MAX_FRAME_BYTES + 1)
    with pytest.raises(ValueError, match="loopback"):
        security_config(allowed_hosts=frozenset({"0.0.0.0:8765"}))


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.5", "localhost", "example.com", "[::1", "::1]"],
)
def test_non_numeric_or_non_loopback_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValueError, match="numeric loopback"):
        validate_loopback_host(host)
    with pytest.raises(ValueError, match="numeric loopback"):
        DevAPIConfig.generate(host=host, port=8765)


def test_ipv6_loopback_and_origin_authority_are_canonicalized() -> None:
    assert validate_loopback_host("[::1]") == "::1"
    assert canonical_authority("[::1]:8765") == "[::1]:8765"
    assert canonical_origin("HTTP://[::1]:8765/") == "http://[::1]:8765"
    generated = DevAPIConfig.generate(host="::1", port=8765)
    assert generated.allowed_hosts == frozenset({"[::1]:8765"})


def test_default_http_port_accepts_canonical_origin_and_host_forms() -> None:
    generated = DevAPIConfig.generate(host="127.0.0.1", port=80)

    assert generated.allowed_origins == frozenset({"http://127.0.0.1"})
    assert generated.allowed_hosts == frozenset({"127.0.0.1", "127.0.0.1:80"})


@pytest.mark.parametrize(
    "origin",
    ["*", "null", "https://127.0.0.1:8765", "http://127.0.0.1:8765/path", "not-an-origin"],
)
def test_ambiguous_or_non_http_origins_are_rejected(origin: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        canonical_origin(origin)


def test_authority_and_origin_reject_embedded_whitespace() -> None:
    with pytest.raises(ValueError, match="authority"):
        canonical_authority("127.0.0.1 :8765")
    with pytest.raises(ValueError, match="origin"):
        canonical_origin("http://127.0.0.1 :8765")


def test_authorize_accepts_only_the_bound_credential() -> None:
    config = security_config()
    security = DevAPISecurity(config, clock=lambda: 101.0)

    principal = security.authorize(raw_headers(config))

    assert principal.session_id == config.session_id
    assert principal.scopes == frozenset({DevAPIScope.chat})
    assert config.session_id not in principal.session_fingerprint


@pytest.mark.parametrize(
    ("header_updates", "status", "code"),
    [
        ({"authorization": None}, 401, "authentication_failed"),
        ({"authorization": "Bearer wrong-token"}, 401, "authentication_failed"),
        ({"authorization": "Bearer é"}, 401, "authentication_failed"),
        ({"origin": "http://attacker.example"}, 403, "origin_forbidden"),
        ({"host": "attacker.example"}, 403, "host_forbidden"),
        ({"x-megumin-protocol": "0"}, 426, "protocol_version_unsupported"),
        ({"x-megumin-session-id": "session_other"}, 403, "session_forbidden"),
        ({"x-megumin-session-id": "é"}, 403, "session_forbidden"),
    ],
)
def test_authorize_attack_matrix_fails_closed_without_echo(
    header_updates: dict[str, str | None],
    status: int,
    code: str,
) -> None:
    config = security_config()
    security = DevAPISecurity(config, clock=lambda: 101.0)

    with pytest.raises(DevAPISecurityError) as captured:
        security.authorize(raw_headers(config, **header_updates))

    assert captured.value.http_status == status
    assert captured.value.code == code
    assert TOKEN not in str(captured.value)
    assert "attacker.example" not in str(captured.value)


def test_duplicate_security_header_is_rejected() -> None:
    config = security_config()
    headers = raw_headers(config)
    headers.append((b"authorization", f"Bearer {TOKEN}".encode("ascii")))

    with pytest.raises(DevAPISecurityError) as captured:
        DevAPISecurity(config, clock=lambda: 101.0).authorize(headers)

    assert captured.value.code == "authentication_failed"
    assert captured.value.reason == "authorization_duplicated"


def test_expired_token_is_rejected() -> None:
    config = security_config()

    with pytest.raises(DevAPISecurityError) as captured:
        DevAPISecurity(config, clock=lambda: 3700.0).authorize(raw_headers(config))

    assert captured.value.code == "authentication_failed"
    assert captured.value.reason == "token_expired"


def test_established_websocket_messages_stop_at_token_expiry() -> None:
    now = [3699.0]
    config = security_config()
    security = DevAPISecurity(config, clock=lambda: now[0])

    assert security.token_seconds_remaining() == 1.0
    security.consume_websocket_message()
    now[0] = 3700.0

    with pytest.raises(DevAPISecurityError) as captured:
        security.consume_websocket_message()

    assert captured.value.code == "authentication_failed"
    assert captured.value.reason == "token_expired"


def test_idle_websocket_is_closed_when_token_expires() -> None:
    class RecordingWebSocket:
        def __init__(self) -> None:
            self.closed: tuple[int, str | None] | None = None

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            self.closed = (code, reason)

    config = security_config()
    security = DevAPISecurity(config, clock=lambda: 3700.0)
    principal = DevAPIPrincipal(config.session_id, config.scopes)
    websocket = RecordingWebSocket()

    asyncio.run(
        _close_websocket_when_token_expires(
            cast(WebSocket, websocket),
            principal,
            security,
        )
    )

    assert websocket.closed == (1008, "authentication_failed")


def test_http_rate_and_concurrency_state_is_bounded_and_recovers() -> None:
    now = [100.0]
    config = security_config(
        http_rate_limit=2,
        http_rate_window_seconds=10.0,
        http_concurrency_limit=1,
    )
    security = DevAPISecurity(config, clock=lambda: now[0])

    security.begin_http_request()
    with pytest.raises(DevAPISecurityError) as concurrent:
        security.begin_http_request()
    assert concurrent.value.code == "connection_limit_exceeded"
    security.end_http_request()

    security.begin_http_request()
    security.end_http_request()
    with pytest.raises(DevAPISecurityError) as limited:
        security.begin_http_request()
    assert limited.value.code == "rate_limit_exceeded"

    now[0] = 111.0
    security.begin_http_request()
    security.end_http_request()


def test_websocket_connection_and_message_limits_recover() -> None:
    now = [100.0]
    config = security_config(
        websocket_connection_limit=1,
        websocket_message_rate_limit=2,
        websocket_message_rate_window_seconds=10.0,
    )
    security = DevAPISecurity(config, clock=lambda: now[0])

    security.begin_websocket()
    with pytest.raises(DevAPISecurityError) as concurrent:
        security.begin_websocket()
    assert concurrent.value.websocket_code == 1013
    security.end_websocket()

    security.consume_websocket_message()
    security.consume_websocket_message()
    with pytest.raises(DevAPISecurityError) as limited:
        security.consume_websocket_message()
    assert limited.value.websocket_code == 1013

    now[0] = 111.0
    security.consume_websocket_message()


def test_security_rejection_logging_is_rate_limited() -> None:
    class RecordingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    config = security_config(security_log_rate_limit=2, security_log_window_seconds=10.0)
    security = DevAPISecurity(config, clock=lambda: 100.0)
    logger = logging.Logger("w04-security-test")
    handler = RecordingHandler()
    logger.addHandler(handler)
    security.set_logger(logger)
    error = DevAPISecurityError(
        "authentication_failed",
        reason="authorization_rejected",
        http_status=401,
    )

    for _ in range(10):
        security.log_rejection(error, transport="http")

    assert len(handler.records) == 2


def test_json_depth_scan_ignores_braces_inside_strings() -> None:
    assert not json_nesting_exceeds(b'{"text":"[[[{{{"}', maximum=2)
    assert json_nesting_exceeds(b'{"a":{"b":{"c":1}}}', maximum=2)


def test_metadata_limits_cover_depth_aggregate_keys_and_nodes() -> None:
    config = security_config(max_metadata_depth=3, max_metadata_keys=2, max_metadata_nodes=4)
    validate_metadata({"a": {"b": 1}}, config)

    for metadata in (
        {"a": {"b": {"c": 1}}},
        {"a": 1, "b": 2, "c": 3},
        {"a": [1, 2, 3]},
    ):
        with pytest.raises(DevAPIProtocolError, match="metadata_limits_exceeded"):
            validate_metadata(metadata, config)

    with pytest.raises(DevAPIProtocolError, match="metadata_limits_exceeded"):
        validate_metadata({"not_finite": float("inf")}, security_config())


def test_user_message_requires_authorized_session_fresh_time_and_bounded_metadata() -> None:
    config = security_config()
    principal = DevAPIPrincipal(config.session_id, config.scopes)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    valid = UserMessage(text="hello", session_id=config.session_id, created_at=now)
    validate_user_message(valid, principal, config, now=now)

    stale = valid.model_copy(update={"created_at": now - timedelta(hours=1)})
    with pytest.raises(DevAPIProtocolError, match="client_time_out_of_range"):
        validate_user_message(stale, principal, config, now=now)

    wrong_session = valid.model_copy(update={"session_id": "session_other"})
    with pytest.raises(DevAPIProtocolError, match="session_forbidden"):
        validate_user_message(wrong_session, principal, config, now=now)

    with pytest.raises(DevAPIProtocolError, match="session_forbidden"):
        ensure_authorized_session(principal, "会话_other")


def test_websocket_command_parser_requires_protocol_v1_object_envelope() -> None:
    valid = parse_websocket_command(
        '{"protocol_version":1,"type":"user.message",'
        '"session_id":"session_w04_test","payload":{"text":"hello"}}'
    )
    assert valid.protocol_version == 1

    for value in (
        "[]",
        '{"protocol_version":0,"type":"user.message","session_id":"session_w04_test","payload":{}}',
        '{"protocol_version":1,"type":"user.message",'
        '"session_id":"session_w04_test","payload":{},"extra":true}',
        '{"protocol_version":1,"type":"user.message",'
        '"session_id":"session_w04_test","payload":{"n":NaN}}',
        '{"protocol_version":1,"type":"user.message","type":"turn.cancel",'
        '"session_id":"session_w04_test","payload":{}}',
    ):
        with pytest.raises(DevAPIProtocolError, match="invalid_command_envelope"):
            parse_websocket_command(value)


def test_streamed_http_body_is_rejected_before_downstream_json_parser() -> None:
    config = security_config(max_http_body_bytes=4)
    security = DevAPISecurity(config, clock=lambda: 101.0)
    downstream_called = False

    async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
        nonlocal downstream_called
        downstream_called = True

    middleware = DevAPIGuardMiddleware(downstream, security=security)
    scope = cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat",
            "raw_path": b"/api/chat",
            "query_string": b"",
            "root_path": "",
            "headers": [*raw_headers(config), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
            "state": {},
        },
    )
    incoming: list[Message] = [
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"45", "more_body": False},
    ]
    outgoing: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0)

    async def send(message: Message) -> None:
        outgoing.append(message)

    asyncio.run(middleware(scope, receive, send))

    assert not downstream_called
    assert outgoing[0]["type"] == "http.response.start"
    assert outgoing[0]["status"] == 413
    assert b"request_body_too_large" in outgoing[1]["body"]
