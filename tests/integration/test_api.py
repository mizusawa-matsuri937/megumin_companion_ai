"""HTTP, WebSocket, shared routing, and lifecycle smoke tests."""

import json
from pathlib import Path

from app.config import Settings
from app.config.settings import LoggingConfig
from app.core import TurnService
from app.main import create_app
from app.schemas import TurnState, UserMessage
from fastapi.testclient import TestClient


class RecordingTurnService(TurnService):
    def __init__(self, original: TurnService) -> None:
        super().__init__(original._logger)
        self.messages: list[UserMessage] = []

    async def accept(self, message: UserMessage) -> TurnState:
        self.messages.append(message)
        return await super().accept(message)


def quiet_settings(*, log_path: Path | None = None) -> Settings:
    return Settings(
        logging=LoggingConfig(
            console_enabled=False,
            file_enabled=log_path is not None,
            file_path=log_path or Path("unused.jsonl"),
        )
    )


def test_health_and_websocket_echo() -> None:
    app = create_app(quiet_settings())
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        with client.websocket_connect("/ws/echo") as websocket:
            websocket.send_text("echo-check")
            assert websocket.receive_text() == "echo-check"


def test_text_and_voice_use_the_same_turn_service() -> None:
    app = create_app(quiet_settings())
    with TestClient(app) as client:
        original = app.state.turn_service
        assert isinstance(original, TurnService)
        recording_service = RecordingTurnService(original)
        app.state.turn_service = recording_service

        with client.websocket_connect("/ws/client") as websocket:
            for mode in ("text", "voice"):
                websocket.send_json(
                    {
                        "type": "user.message",
                        "payload": {"text": f"来自 {mode}", "input_mode": mode},
                    }
                )
                event = websocket.receive_json()
                assert event["type"] == "turn.accepted"
                assert event["payload"]["input_mode"] == mode
                assert event["payload"]["status"] == "accepted"

        assert [message.input_mode.value for message in recording_service.messages] == [
            "text",
            "voice",
        ]


def test_http_chat_uses_normalized_user_message() -> None:
    app = create_app(quiet_settings())
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"text": "  显式发送  ", "input_mode": "text"})

    assert response.status_code == 200
    assert response.json()["input_mode"] == "text"
    assert response.json()["status"] == "accepted"


def test_invalid_payload_does_not_echo_private_input() -> None:
    private_input = "private-draft@example.com"
    app = create_app(quiet_settings())
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={"text": "valid", "input_mode": private_input},
        )

    assert response.status_code == 422
    assert private_input not in response.text


def test_lifecycle_writes_start_and_stop_events(tmp_path: Path) -> None:
    log_path = tmp_path / "lifecycle.jsonl"
    app = create_app(quiet_settings(log_path=log_path))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    events = [
        json.loads(line)["event"] for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["application.started", "application.stopped"]
