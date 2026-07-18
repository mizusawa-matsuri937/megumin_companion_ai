"""HTTP, WebSocket, shared routing, and lifecycle smoke tests."""

import asyncio
import json
from pathlib import Path

import pytest
from app.api.security import DevAPIConfig, DevAPIScope
from app.config import Settings
from app.config.settings import LLMConfig, LoggingConfig, PipelineConfig
from app.core import TurnService
from app.main import _settle_resource_close, create_app
from app.paths import AppPaths
from app.schemas import TurnState, UserMessage
from fastapi import FastAPI
from fastapi.testclient import TestClient

DEV_API = DevAPIConfig(
    token="integration-api-token-000000000000000000000000",
    session_id="session_integration_api",
    allowed_origins=frozenset({"http://127.0.0.1:8765"}),
    allowed_hosts=frozenset({"127.0.0.1:8765"}),
    scopes=frozenset({DevAPIScope.chat, DevAPIScope.admin}),
)
WS_BASE_URL = "ws://127.0.0.1:8765"


def secured_app(settings: Settings) -> FastAPI:
    return create_app(settings, dev_api=DEV_API)


def secured_client(application: FastAPI) -> TestClient:
    return TestClient(
        application,
        base_url="http://127.0.0.1:8765",
        headers=DEV_API.client_headers(),
        client=("127.0.0.1", 51001),
    )


def command(command_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "type": command_type,
        "session_id": DEV_API.session_id,
        "payload": payload,
    }


class RecordingTurnService(TurnService):
    def __init__(self, original: TurnService) -> None:
        super().__init__(original._logger)
        self.messages: list[UserMessage] = []

    async def accept(self, message: UserMessage) -> TurnState:
        self.messages.append(message)
        return await super().accept(message)


def quiet_settings(*, root: Path, log_path: Path | None = None) -> Settings:
    settings = Settings(
        logging=LoggingConfig(
            console_enabled=False,
            file_enabled=log_path is not None,
            file_path=Path(log_path.name) if log_path is not None else Path("unused.jsonl"),
        )
    )
    settings._paths = AppPaths(root=root)
    return settings


def mock_pipeline_settings(*, root: Path, token_delay_ms: int = 0) -> Settings:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        llm=LLMConfig(provider="mock"),
        pipeline=PipelineConfig(
            mock_token_delay_ms=token_delay_ms,
            mock_audio_duration_ms=0,
        ),
    )
    settings._paths = AppPaths(root=root)
    return settings


def test_health_and_websocket_echo(tmp_path: Path) -> None:
    app = secured_app(quiet_settings(root=tmp_path / "app"))
    with secured_client(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

        with client.websocket_connect(f"{WS_BASE_URL}/ws/echo") as websocket:
            websocket.send_text("echo-check")
            assert websocket.receive_text() == "echo-check"
            websocket.send_bytes(b"binary-check")
            assert websocket.receive_bytes() == b"binary-check"

        state = client.get("/debug/state")
        assert state.status_code == 200
        assert state.json()["active_turns"] == []


def test_text_and_voice_use_the_same_turn_service(tmp_path: Path) -> None:
    app = secured_app(quiet_settings(root=tmp_path / "app"))
    with secured_client(app) as client:
        original = app.state.turn_service
        assert isinstance(original, TurnService)
        recording_service = RecordingTurnService(original)
        app.state.turn_service = recording_service

        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            for mode in ("text", "voice"):
                websocket.send_json(
                    command(
                        "user.message",
                        {
                            "text": f"来自 {mode}",
                            "input_mode": mode,
                            "session_id": DEV_API.session_id,
                        },
                    )
                )
                event = websocket.receive_json()
                assert event["type"] == "turn.accepted"
                assert event["payload"]["input_mode"] == mode
                assert event["payload"]["status"] == "accepted"

        assert [message.input_mode.value for message in recording_service.messages] == [
            "text",
            "voice",
        ]


def test_http_chat_uses_normalized_user_message(tmp_path: Path) -> None:
    app = secured_app(quiet_settings(root=tmp_path / "app"))
    with secured_client(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "text": "  显式发送  ",
                "input_mode": "text",
                "session_id": DEV_API.session_id,
            },
        )

    assert response.status_code == 200
    assert response.json()["input_mode"] == "text"
    assert response.json()["status"] == "accepted"


def test_invalid_payload_does_not_echo_private_input(tmp_path: Path) -> None:
    private_input = "private-draft@example.com"
    app = secured_app(quiet_settings(root=tmp_path / "app"))
    with secured_client(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "text": "valid",
                "input_mode": private_input,
                "session_id": DEV_API.session_id,
            },
        )

    assert response.status_code == 422
    assert private_input not in response.text


def test_lifecycle_writes_start_and_stop_events(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "lifecycle.jsonl"
    app = secured_app(quiet_settings(root=tmp_path, log_path=log_path))

    with secured_client(app) as client:
        assert client.get("/health").status_code == 200

    actual_log = next(log_path.parent.glob("lifecycle.main.*.jsonl"))
    events = [
        json.loads(line)["event"] for line in actual_log.read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["application.started", "application.stopped"]


def test_websocket_streams_complete_mock_pipeline_for_text_and_voice(tmp_path: Path) -> None:
    app = secured_app(mock_pipeline_settings(root=tmp_path / "app"))
    with (
        secured_client(app) as client,
        client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket,
    ):
        for mode in ("text", "voice"):
            websocket.send_json(
                command(
                    "user.message",
                    {
                        "text": f"运行 {mode} 完整链路",
                        "input_mode": mode,
                        "session_id": DEV_API.session_id,
                    },
                )
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


def test_http_interrupt_cancels_active_turn(tmp_path: Path) -> None:
    app = secured_app(mock_pipeline_settings(root=tmp_path / "app", token_delay_ms=100))
    with secured_client(app) as client:
        accepted = client.post(
            "/api/chat",
            json={"text": "请开始一个较慢的回复", "session_id": DEV_API.session_id},
        ).json()
        cancelled = client.post(
            "/api/interrupt",
            json={"turn_id": accepted["turn_id"], "session_id": accepted["session_id"]},
        )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_websocket_rejects_malformed_commands_without_private_echo(tmp_path: Path) -> None:
    private = "private-command@example.com"
    app = secured_app(quiet_settings(root=tmp_path / "app"))
    with (
        secured_client(app) as client,
        client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket,
    ):
        websocket.send_json(["not", "an", "object"])
        assert websocket.receive_json()["error"]["code"] == "invalid_command_envelope"

        websocket.send_json(command("unknown", {"text": private}))
        unsupported = websocket.receive_json()
        assert unsupported["error"]["code"] == "unsupported_message_type"
        assert private not in json.dumps(unsupported)

        websocket.send_json(command("turn.cancel", {"session_id": ""}))
        invalid_cancel = websocket.receive_json()
        assert invalid_cancel["error"]["code"] == "invalid_turn_cancel"
        assert "input" not in json.dumps(invalid_cancel)

        websocket.send_json(
            command("user.message", {"text": "   ", "session_id": DEV_API.session_id})
        )
        invalid_message = websocket.receive_json()
        assert invalid_message["error"]["code"] == "invalid_user_message"
        assert "input" not in json.dumps(invalid_message)

        websocket.send_json(command("turn.cancel", {"session_id": DEV_API.session_id}))
        websocket.send_json(
            command(
                "user.message",
                {
                    "text": "错误后仍可继续",
                    "input_mode": "text",
                    "session_id": DEV_API.session_id,
                },
            )
        )
        assert websocket.receive_json()["type"] == "turn.accepted"


def test_partial_startup_closes_already_started_event_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Sink:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    sink = Sink()

    def fail_pipeline(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr("app.main.build_vts_event_sink", lambda _settings: sink)
    monkeypatch.setattr("app.main.build_dialogue_pipeline", fail_pipeline)

    with (
        pytest.raises(RuntimeError, match="startup failure"),
        secured_client(secured_app(quiet_settings(root=tmp_path / "app"))),
    ):
        pass
    assert sink.closed == 1


def test_lifespan_close_drains_resource_after_repeated_outer_cancellation() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = False

        async def closer() -> None:
            nonlocal finished
            entered.set()
            await release.wait()
            finished = True

        settling = asyncio.create_task(_settle_resource_close(closer))
        await entered.wait()
        settling.cancel()
        await asyncio.sleep(0)
        settling.cancel()
        await asyncio.sleep(0)
        assert not settling.done()
        release.set()
        cancelled, failure = await settling
        assert cancelled is not None
        assert failure is None
        assert finished

    asyncio.run(scenario())
