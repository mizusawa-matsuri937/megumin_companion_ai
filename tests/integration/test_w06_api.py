"""W06 HTTP/WebSocket idempotency, replay, and fail-closed integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from itertools import count
from pathlib import Path
from typing import Any

from app.api.security import DevAPIConfig, DevAPIScope
from app.config import Settings
from app.config.settings import LLMConfig, LoggingConfig, PipelineConfig, StorageConfig
from app.main import create_app
from app.paths import AppPaths
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
