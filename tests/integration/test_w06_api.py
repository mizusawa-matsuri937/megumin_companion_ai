"""W06 HTTP/WebSocket idempotency, replay, and fail-closed integration tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from app.api.security import DevAPIConfig, DevAPIScope
from app.config import Settings
from app.config.settings import LLMConfig, LoggingConfig, PipelineConfig, StorageConfig
from app.core.idempotency import IdempotencyKey
from app.main import create_app
from app.paths import AppPaths
from app.schemas import InputMode, TurnState, TurnStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

DEV_API = DevAPIConfig(
    token="w06-integration-token-000000000000000000000000",
    client_id="client_w06_integration",
    session_id="session_w06_integration",
    allowed_origins=frozenset({"http://127.0.0.1:8765"}),
    allowed_hosts=frozenset({"127.0.0.1:8765"}),
    scopes=frozenset({DevAPIScope.chat, DevAPIScope.admin}),
)
WS_BASE_URL = "ws://127.0.0.1:8765"
_COMMAND_IDS = count()


def _settings(root: Path, *, storage_enabled: bool = True) -> Settings:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(
            enabled=storage_enabled,
            database_path=Path("companion.sqlite3"),
        ),
        llm=LLMConfig(provider="mock"),
        pipeline=PipelineConfig(mock_token_delay_ms=5, mock_audio_duration_ms=0),
    )
    settings._paths = AppPaths(root=root)
    return settings


def _app(root: Path, *, storage_enabled: bool = True) -> FastAPI:
    return create_app(_settings(root, storage_enabled=storage_enabled), dev_api=DEV_API)


def _client(application: FastAPI) -> TestClient:
    return TestClient(
        application,
        base_url="http://127.0.0.1:8765",
        headers=DEV_API.client_headers(),
        client=("127.0.0.1", 51006),
    )


def _command(command_type: str, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "command_id": f"command-{next(_COMMAND_IDS)}",
        "client_id": DEV_API.client_id,
        "session_id": DEV_API.session_id,
        "type": command_type,
        "payload": dict(payload),
    }


def test_http_duplicate_returns_original_turn_and_changed_body_conflicts(tmp_path: Path) -> None:
    application = _app(tmp_path / "app")
    payload = {
        "message_id": "message-http-idempotent",
        "session_id": DEV_API.session_id,
        "text": "W06 HTTP private duplicate sentinel",
    }

    with _client(application) as client:
        first = client.post("/api/chat", json=payload)
        duplicate = client.post("/api/chat", json=payload)
        conflict = client.post("/api/chat", json={**payload, "text": "changed"})

        assert first.status_code == duplicate.status_code == 200
        assert first.json()["turn_id"] == duplicate.json()["turn_id"]
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "idempotency_conflict"}
        assert "W06 HTTP private duplicate sentinel" not in conflict.text
        with application.state.memory_runtime.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM idempotency_turns").fetchone()[0] == 1


def test_storage_disabled_rejects_before_turn_or_pipeline_creation(tmp_path: Path) -> None:
    application = _app(tmp_path / "disabled", storage_enabled=False)

    with _client(application) as client:
        rejected = client.post(
            "/api/chat",
            json={
                "message_id": "message-storage-disabled",
                "session_id": DEV_API.session_id,
                "text": "must not reach provider",
            },
        )
        state = client.get("/debug/state")

    assert rejected.status_code == 503
    assert rejected.json() == {"detail": "idempotency_unavailable"}
    assert state.json()["turns"] == {}


def test_interrupt_checks_session_then_fails_closed_without_idempotency(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path / "disabled", storage_enabled=False)

    with _client(application) as client:
        forbidden = client.post(
            "/api/interrupt",
            json={"turn_id": "turn-private-sentinel", "session_id": "session_other"},
        )
        unavailable = client.post(
            "/api/interrupt",
            json={
                "turn_id": "turn-private-sentinel",
                "session_id": DEV_API.session_id,
            },
        )

    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "session_forbidden"}
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "idempotency_unavailable"}
    assert "turn-private-sentinel" not in forbidden.text + unavailable.text


def test_websocket_resume_fails_closed_without_idempotency(tmp_path: Path) -> None:
    application = _app(tmp_path / "disabled", storage_enabled=False)

    with (
        _client(application) as client,
        client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket,
    ):
        websocket.send_json(_command("session.resume", {"last_seq": 0}))
        error = websocket.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()

    legacy_headers = DEV_API.client_headers()
    legacy_headers.pop("X-Megumin-Client-ID")
    with (
        TestClient(
            application,
            base_url="http://127.0.0.1:8765",
            headers=legacy_headers,
            client=("127.0.0.1", 51007),
        ) as legacy_client,
        legacy_client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket,
    ):
        websocket.send_json(
            {
                "protocol_version": 1,
                "session_id": DEV_API.session_id,
                "type": "user.message",
                "payload": {
                    "message_id": "message-disabled-legacy",
                    "session_id": DEV_API.session_id,
                    "text": "private legacy sentinel",
                },
            }
        )
        legacy_error = websocket.receive_json()
        with pytest.raises(WebSocketDisconnect) as legacy_closed:
            websocket.receive_json()

    assert error["error"]["code"] == "idempotency_unavailable"
    assert closed.value.code == 1013
    assert legacy_error["error"]["code"] == "idempotency_unavailable"
    assert "private legacy sentinel" not in json.dumps(legacy_error)
    assert legacy_closed.value.code == 1013


def test_websocket_reconnect_replays_original_event_then_resend_returns_snapshot(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path / "app")
    message = {
        "message_id": "message-websocket-replay",
        "session_id": DEV_API.session_id,
        "text": "replay without replaying audio",
    }

    with _client(application) as client:
        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_json(_command("session.resume", {"last_seq": 0}))
            websocket.send_json(_command("user.message", message))
            events: list[dict[str, Any]] = []
            while True:
                event = websocket.receive_json()
                events.append(event)
                if event["type"] == "assistant.completed":
                    break

        seqs = [int(event["seq"]) for event in events]
        assert seqs == sorted(set(seqs))
        assert all(event.get("event_id") for event in events)
        accepted = next(event for event in events if event["type"] == "turn.accepted")
        original_terminal = events[-1]

        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_json(
                _command("session.resume", {"last_seq": int(original_terminal["seq"]) - 1})
            )
            replayed = websocket.receive_json()
            assert replayed["seq"] == original_terminal["seq"]
            assert replayed["event_id"] == original_terminal["event_id"]
            assert replayed["type"] == "assistant.completed"

            websocket.send_json(_command("user.message", message))
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "turn.snapshot"
            assert snapshot["turn_id"] == accepted["turn_id"]
            assert snapshot["payload"]["status"] == "completed"

        debug = client.get("/debug/state").json()
        assert list(debug["turns"]) == [accepted["turn_id"]]


def test_websocket_large_authoritative_snapshot_is_chunked_below_frame_limit(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path / "chunked")
    now = datetime.now(UTC)

    async def populate() -> None:
        store = application.state.idempotency_store
        for index in range(200):
            suffix = f"{index:03d}"
            message_id = f"message-{suffix}-" + "m" * 116
            turn_id = f"turn-{suffix}-" + "t" * 119
            state = TurnState(
                turn_id=turn_id,
                session_id=DEV_API.session_id,
                source_message_id=message_id,
                input_mode=InputMode.text,
                created_at=now,
                updated_at=now,
            )
            await store.claim(
                IdempotencyKey(DEV_API.client_id, DEV_API.session_id, message_id),
                fingerprint=f"{index:064x}",
                state=state,
                now=now,
            )
            await store.update(
                client_id=DEV_API.client_id,
                state=state.model_copy(update={"status": TurnStatus.completed}),
                now=now,
            )

    with _client(application) as client:
        assert client.portal is not None
        client.portal.call(populate)
        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_json(_command("session.resume", {"last_seq": 999_999}))
            reset = websocket.receive_json()
            assert reset["type"] == "session.reset"
            assert reset["snapshot"]["turn_count"] == 200
            chunks = [
                websocket.receive_json() for _index in range(int(reset["snapshot"]["chunk_count"]))
            ]

        assert all(chunk["type"] == "session.snapshot.chunk" for chunk in chunks)
        assert sum(len(chunk["turns"]) for chunk in chunks) == 200
        assert all(
            len(json.dumps(chunk, ensure_ascii=False).encode("utf-8"))
            <= DEV_API.max_websocket_frame_bytes
            for chunk in chunks
        )


def test_authenticated_w04_websocket_shape_is_read_for_one_version(tmp_path: Path) -> None:
    application = _app(tmp_path / "legacy")
    headers = DEV_API.client_headers()
    headers.pop("X-Megumin-Client-ID")
    message = {
        "message_id": "message-legacy-transition",
        "session_id": DEV_API.session_id,
        "text": "legacy transition",
    }
    legacy_command = {
        "protocol_version": 1,
        "session_id": DEV_API.session_id,
        "type": "user.message",
        "payload": message,
    }

    with TestClient(
        application,
        base_url="http://127.0.0.1:8765",
        headers=headers,
        client=("127.0.0.1", 51006),
    ) as client:
        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_json(legacy_command)
            accepted = websocket.receive_json()
            assert accepted["type"] == "turn.accepted"

        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_json(legacy_command)
            while True:
                event = websocket.receive_json()
                if event["type"] == "turn.snapshot":
                    assert event["turn_id"] == accepted["turn_id"]
                    break
