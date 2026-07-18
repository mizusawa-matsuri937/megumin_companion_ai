"""W04 HTTP/WebSocket attack matrix and fail-closed integration tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.api.security import DevAPIConfig, DevAPIScope
from app.config import Settings
from app.config.logging import log_event
from app.config.settings import LoggingConfig, StorageConfig
from app.main import create_app
from app.paths import AppPaths
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

BASE_URL = "http://127.0.0.1:8765"
WS_BASE_URL = "ws://127.0.0.1:8765"
BASE_CONFIG = DevAPIConfig(
    token="w04-integration-token-00000000000000000000000",
    client_id="client_w04_integration",
    session_id="session_w04_integration",
    allowed_origins=frozenset({BASE_URL}),
    allowed_hosts=frozenset({"127.0.0.1:8765"}),
    scopes=frozenset({DevAPIScope.chat, DevAPIScope.admin}),
)


def quiet_settings(root: Path, *, log_path: Path | None = None) -> Settings:
    settings = Settings(
        logging=LoggingConfig(
            console_enabled=False,
            file_enabled=log_path is not None,
            file_path=Path(log_path.name) if log_path is not None else Path("unused.jsonl"),
        ),
        storage=StorageConfig(enabled=True, database_path=Path("companion.sqlite3")),
    )
    settings._paths = AppPaths(root=root)
    return settings


def secured_app(
    root: Path,
    *,
    config: DevAPIConfig = BASE_CONFIG,
    log_path: Path | None = None,
) -> FastAPI:
    return create_app(quiet_settings(root, log_path=log_path), dev_api=config)


def client_for(
    application: FastAPI,
    *,
    config: DevAPIConfig = BASE_CONFIG,
    authenticated: bool = True,
) -> TestClient:
    return TestClient(
        application,
        base_url=BASE_URL,
        headers=config.client_headers() if authenticated else None,
        client=("127.0.0.1", 51003),
    )


def command(
    config: DevAPIConfig,
    command_type: str,
    payload: dict[str, object],
    *,
    session_id: str | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "command_id": f"command-{command_type}-{id(payload)}",
        "client_id": config.client_id,
        "type": command_type,
        "session_id": session_id or config.session_id,
        "payload": payload,
    }


def test_factory_without_explicit_dev_api_credentials_is_locked(tmp_path: Path) -> None:
    application = create_app(quiet_settings(tmp_path / "locked"))

    with TestClient(application, base_url=BASE_URL, client=("127.0.0.1", 51004)) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "dev_api_unavailable"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_code"),
    [
        ({}, 401, "authentication_failed"),
        ({"Authorization": "Bearer attacker-private-token"}, 401, "authentication_failed"),
        ({"Origin": "http://attacker.example"}, 403, "origin_forbidden"),
        ({"Host": "attacker.example"}, 403, "host_forbidden"),
        ({"X-Megumin-Protocol": "0"}, 426, "protocol_version_unsupported"),
        ({"X-Megumin-Client-ID": "client_other"}, 403, "client_identity_forbidden"),
        ({"X-Megumin-Session-ID": "session_other"}, 403, "session_forbidden"),
    ],
)
def test_http_auth_origin_host_protocol_and_session_attack_matrix(
    tmp_path: Path,
    headers: dict[str, str],
    expected_status: int,
    expected_code: str,
) -> None:
    private = "attacker-private-token"
    application = secured_app(tmp_path / "app")
    valid = BASE_CONFIG.client_headers()
    if headers:
        valid.update(headers)
    else:
        valid.pop("Authorization")

    with client_for(application, authenticated=False) as client:
        response = client.get("/health", headers=valid)

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_code}
    assert private not in response.text
    assert BASE_CONFIG.token not in response.text


def test_non_loopback_peer_is_rejected_even_with_valid_credentials(tmp_path: Path) -> None:
    application = secured_app(tmp_path / "app")

    with TestClient(
        application,
        base_url=BASE_URL,
        headers=BASE_CONFIG.client_headers(),
        client=("192.0.2.10", 51005),
    ) as client:
        response = client.get("/health")
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect(f"{WS_BASE_URL}/ws/client"),
        ):
            pass

    assert response.status_code == 403
    assert response.json() == {"detail": "client_forbidden"}
    assert closed.value.code == 1008


def test_http_valid_response_is_no_store_and_docs_are_not_exposed(tmp_path: Path) -> None:
    application = secured_app(tmp_path / "app")

    with client_for(application) as client:
        health = client.get("/health")
        docs = client.get("/docs")

    assert health.status_code == 200
    assert health.json()["status"] == "ready"
    assert health.headers["cache-control"] == "no-store"
    assert health.headers["x-megumin-protocol"] == "1"
    assert docs.status_code == 404


def test_chat_scope_cannot_use_admin_routes_and_cross_session_fails(tmp_path: Path) -> None:
    config = replace(BASE_CONFIG, scopes=frozenset({DevAPIScope.chat}))
    application = secured_app(tmp_path / "app", config=config)

    with client_for(application, config=config) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/debug/state").json() == {"detail": "scope_forbidden"}
        assert client.get("/api/features").json() == {"detail": "scope_forbidden"}
        cross_session = client.post(
            "/api/chat",
            json={"text": "private cross-session body", "session_id": "session_other"},
        )
        accepted = client.post(
            "/api/chat",
            json={"text": "authorized", "session_id": config.session_id},
        )

    assert cross_session.status_code == 403
    assert cross_session.json() == {"detail": "session_forbidden"}
    assert "private cross-session body" not in cross_session.text
    assert accepted.status_code == 200


def test_client_time_metadata_and_identifier_limits_fail_without_echo(tmp_path: Path) -> None:
    application = secured_app(tmp_path / "app")
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    cases = [
        {
            "text": "private stale content",
            "session_id": BASE_CONFIG.session_id,
            "created_at": stale,
        },
        {
            "text": "private metadata content",
            "session_id": BASE_CONFIG.session_id,
            "metadata": {"a": {"b": {"c": {"d": {"e": 1}}}}},
        },
        {
            "text": "private identifier content",
            "session_id": BASE_CONFIG.session_id,
            "message_id": "m" * 129,
        },
    ]

    with client_for(application) as client:
        responses = [client.post("/api/chat", json=payload) for payload in cases]

    assert [response.status_code for response in responses] == [422, 422, 422]
    combined = "".join(response.text for response in responses)
    assert "private stale content" not in combined
    assert "private metadata content" not in combined
    assert "private identifier content" not in combined


def test_body_and_json_depth_are_rejected_before_route_processing(tmp_path: Path) -> None:
    private = "private-oversized-sentinel"
    config = replace(BASE_CONFIG, max_http_body_bytes=256, max_json_depth=4)
    application = secured_app(tmp_path / "app", config=config)
    oversized = json.dumps(
        {"text": private * 32, "session_id": config.session_id},
        separators=(",", ":"),
    )
    nested = '{"a":{"b":{"c":{"d":{"e":1}}}}}'

    with client_for(application, config=config) as client:
        too_large = client.post(
            "/api/chat",
            content=oversized,
            headers={"Content-Type": "application/json"},
        )
        too_deep = client.post(
            "/api/chat",
            content=nested,
            headers={"Content-Type": "application/vnd.megumin+json"},
        )
        state = client.get("/debug/state")

    assert too_large.status_code == 413
    assert too_large.json() == {"detail": "request_body_too_large"}
    assert private not in too_large.text
    assert too_deep.status_code == 400
    assert too_deep.json() == {"detail": "json_nesting_too_deep"}
    assert state.json()["active_turns"] == []


def test_http_rate_limit_is_enforced_with_stable_retry_header(tmp_path: Path) -> None:
    config = replace(BASE_CONFIG, http_rate_limit=2, http_rate_window_seconds=30.0)
    application = secured_app(tmp_path / "app", config=config)

    with client_for(application, config=config) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
        limited = client.get("/health")

    assert limited.status_code == 429
    assert limited.json() == {"detail": "rate_limit_exceeded"}
    assert limited.headers["retry-after"] == "30"


@pytest.mark.parametrize(
    "header_update",
    [
        {"Authorization": "Bearer wrong-websocket-token"},
        {"Origin": "http://attacker.example"},
        {"X-Megumin-Protocol": "0"},
        {"X-Megumin-Client-ID": "client_other"},
        {"X-Megumin-Session-ID": "session_other"},
    ],
)
def test_websocket_handshake_rejects_attacker_before_accept(
    tmp_path: Path,
    header_update: dict[str, str],
) -> None:
    application = secured_app(tmp_path / "app")
    headers = BASE_CONFIG.client_headers()
    headers.update(header_update)

    with (
        client_for(application, authenticated=False) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"{WS_BASE_URL}/ws/client", headers=headers),
    ):
        pass


def test_websocket_protocol_v1_subscribes_only_authorized_identity(tmp_path: Path) -> None:
    application = secured_app(tmp_path / "app")

    with (
        client_for(application) as client,
        client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket,
    ):
        websocket.send_json(command(BASE_CONFIG, "session.resume", {"last_seq": 0}))
        service = application.state.turn_service
        websocket.send_json(
            command(
                BASE_CONFIG,
                "user.message",
                {"text": "authorized websocket", "session_id": BASE_CONFIG.session_id},
            )
        )
        accepted = websocket.receive_json()
        assert (BASE_CONFIG.client_id, BASE_CONFIG.session_id) in service._subscribers

    assert accepted["protocol_version"] == 1
    assert accepted["type"] == "turn.accepted"
    assert accepted["session_id"] == BASE_CONFIG.session_id


def test_websocket_cross_session_command_returns_safe_error_then_closes(tmp_path: Path) -> None:
    private = "private-cross-session-websocket"
    application = secured_app(tmp_path / "app")

    with (
        client_for(application) as client,
        client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket,
    ):
        websocket.send_json(
            command(
                BASE_CONFIG,
                "user.message",
                {"text": private, "session_id": "session_other"},
                session_id="session_other",
            )
        )
        error = websocket.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()

    assert error["error"]["code"] == "session_forbidden"
    assert private not in json.dumps(error)
    assert closed.value.code == 1008


def test_websocket_oversized_binary_flood_and_connection_limits(tmp_path: Path) -> None:
    oversized_config = replace(BASE_CONFIG, max_websocket_frame_bytes=64)
    oversized_app = secured_app(tmp_path / "oversized", config=oversized_config)
    with client_for(oversized_app, config=oversized_config) as client:
        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_text("x" * 65)
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        assert closed.value.code == 1009

    binary_app = secured_app(tmp_path / "binary")
    with client_for(binary_app) as client:
        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_bytes(b"binary-command")
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        assert closed.value.code == 1003

    flood_config = replace(BASE_CONFIG, websocket_message_rate_limit=1)
    flood_app = secured_app(tmp_path / "flood", config=flood_config)
    with client_for(flood_app, config=flood_config) as client:
        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_text("not-json")
            assert websocket.receive_json()["error"]["code"] == "invalid_command_envelope"
            websocket.send_text("not-json-again")
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        assert closed.value.code == 1013

    connection_config = replace(BASE_CONFIG, websocket_connection_limit=1)
    connection_app = secured_app(tmp_path / "connection", config=connection_config)
    with (
        client_for(connection_app, config=connection_config) as client,
        client.websocket_connect(f"{WS_BASE_URL}/ws/client"),
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect(f"{WS_BASE_URL}/ws/client"),
    ):
        pass
    assert closed.value.code == 1013


def test_debug_echo_requires_admin_scope(tmp_path: Path) -> None:
    chat_only = replace(BASE_CONFIG, scopes=frozenset({DevAPIScope.chat}))
    application = secured_app(tmp_path / "app", config=chat_only)

    with (
        client_for(application, config=chat_only) as client,
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect(f"{WS_BASE_URL}/ws/echo"),
    ):
        pass

    assert closed.value.code == 1008


def test_security_logs_use_reason_codes_and_redact_runtime_credentials(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "security.jsonl"
    application = secured_app(tmp_path, log_path=log_path)
    invalid_headers = BASE_CONFIG.client_headers()
    invalid_headers["Origin"] = "http://attacker.example"

    with client_for(application, authenticated=False) as client:
        assert client.get("/health", headers=invalid_headers).status_code == 403
        log_event(
            application.state.logger,
            30,
            "synthetic.credential.redaction",
            accidental_value=(
                f"{BASE_CONFIG.token}:{BASE_CONFIG.client_id}:{BASE_CONFIG.session_id}"
            ),
        )

    content = log_path.read_text(encoding="utf-8")
    assert "origin_not_allowed" in content
    assert "attacker.example" not in content
    assert BASE_CONFIG.token not in content
    assert BASE_CONFIG.client_id not in content
    assert BASE_CONFIG.session_id not in content
    assert "[REDACTED]" in content
