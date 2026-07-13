"""HTTP, WebSocket, shared routing, and lifecycle smoke tests."""

import json
from pathlib import Path

from app.config import Settings
from app.config.settings import LLMConfig, LoggingConfig, PipelineConfig
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


def mock_pipeline_settings(*, token_delay_ms: int = 0) -> Settings:
    return Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        llm=LLMConfig(provider="mock"),
        pipeline=PipelineConfig(
            mock_token_delay_ms=token_delay_ms,
            mock_audio_duration_ms=0,
        ),
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
            websocket.send_bytes(b"binary-check")
            assert websocket.receive_bytes() == b"binary-check"

        state = client.get("/debug/state")
        assert state.status_code == 200
        assert state.json()["active_turns"] == []


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


def test_websocket_streams_complete_mock_pipeline_for_text_and_voice() -> None:
    app = create_app(mock_pipeline_settings())
    with TestClient(app) as client, client.websocket_connect("/ws/client") as websocket:
        for mode in ("text", "voice"):
            websocket.send_json(
                {
                    "type": "user.message",
                    "payload": {"text": f"运行 {mode} 完整链路", "input_mode": mode},
                }
            )
            events = []
            while True:
                event = websocket.receive_json()
                events.append(event)
                if event["type"] == "assistant.completed":
                    break

            event_types = [event["type"] for event in events]
            assert event_types[0] == "turn.accepted"
            assert "assistant.delta" in event_types
            assert "assistant.segment" in event_types
            assert "audio.ready" in event_types
            assert "playback.started" in event_types
            assert event_types[-1] == "assistant.completed"
            assert events[0]["payload"]["input_mode"] == mode
            metrics = events[-1]["payload"]["metrics"]
            assert metrics["llm_first_token_ms"] is not None
            assert metrics["first_sentence_play_ms"] is not None
            segment = next(event for event in events if event["type"] == "assistant.segment")
            assert segment["payload"]["emotion"] in {
                "neutral",
                "happy",
                "shy",
                "proud",
                "worried",
                "excited",
                "focused",
            }
            assert 0 < segment["payload"]["tts_speed_factor"] <= 3
            assert isinstance(segment["payload"]["expression_update"], bool)


def test_http_interrupt_cancels_active_turn() -> None:
    app = create_app(mock_pipeline_settings(token_delay_ms=100))
    with TestClient(app) as client:
        accepted = client.post("/api/chat", json={"text": "请开始一个较慢的回复"}).json()
        cancelled = client.post(
            "/api/interrupt",
            json={"turn_id": accepted["turn_id"], "session_id": accepted["session_id"]},
        )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_websocket_rejects_malformed_commands_without_private_echo() -> None:
    private = "private-command@example.com"
    app = create_app(quiet_settings())
    with TestClient(app) as client, client.websocket_connect("/ws/client") as websocket:
        websocket.send_json(["not", "an", "object"])
        assert websocket.receive_json()["error"]["code"] == "unsupported_message_type"

        websocket.send_json({"type": "unknown", "payload": {"text": private}})
        unsupported = websocket.receive_json()
        assert unsupported["error"]["code"] == "unsupported_message_type"
        assert private not in json.dumps(unsupported)

        websocket.send_json({"type": "turn.cancel", "payload": {"session_id": ""}})
        invalid_cancel = websocket.receive_json()
        assert invalid_cancel["error"]["code"] == "invalid_turn_cancel"
        assert "input" not in json.dumps(invalid_cancel)

        websocket.send_json({"type": "user.message", "payload": {"text": "   "}})
        invalid_message = websocket.receive_json()
        assert invalid_message["error"]["code"] == "invalid_user_message"
        assert "input" not in json.dumps(invalid_message)

        websocket.send_json({"type": "turn.cancel", "payload": {}})
        websocket.send_json(
            {"type": "user.message", "payload": {"text": "错误后仍可继续", "input_mode": "text"}}
        )
        assert websocket.receive_json()["type"] == "turn.accepted"
