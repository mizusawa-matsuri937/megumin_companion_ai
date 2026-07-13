"""Private-state API, turn persistence, confirmation, and deletion integration."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.config import Settings
from app.config.settings import (
    LLMConfig,
    LoggingConfig,
    MemoryConfig,
    PipelineConfig,
    StorageConfig,
)
from app.core.cancellation import CancellationToken
from app.main import create_app
from app.memory import (
    MemoryClaim,
    MemoryDecision,
    MemorySensitivity,
    MemoryType,
    SourceInputMode,
)
from app.memory.runtime import MemoryRuntime
from app.schemas import ChatCompletion, ChatRequest
from fastapi.testclient import TestClient


class CandidateLLM:
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.closed = 0

    async def stream(self, request: ChatRequest, token: CancellationToken) -> AsyncIterator[str]:
        token.raise_if_cancelled()
        yield "unused"

    async def complete(self, request: ChatRequest, token: CancellationToken) -> ChatCompletion:
        token.raise_if_cancelled()
        self.requests.append(request)
        return ChatCompletion(
            text=(
                '{"claims":[{"memory_type":"fact",'
                '"canonical_key":"preference:coffee",'
                '"content":"用户喜欢手冲咖啡",'
                '"evidence_quote":"我喜欢手冲咖啡",'
                '"importance_score":0.8,"confidence_score":0.95,'
                '"sensitivity_hint":"normal","related_emotion":null}]}'
            )
        )

    async def close(self) -> None:
        self.closed += 1


def stateful_settings(database_path: Path) -> Settings:
    return Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        llm=LLMConfig(provider="mock"),
        storage=StorageConfig(enabled=True, database_path=database_path),
        memory=MemoryConfig(history_retention_days=7, confirmation_ttl_minutes=15),
        pipeline=PipelineConfig(mock_token_delay_ms=10, mock_audio_duration_ms=0),
    )


def claim(
    *,
    content: str,
    evidence: str,
    canonical_key: str,
    sensitivity: MemorySensitivity = MemorySensitivity.normal,
) -> MemoryClaim:
    return MemoryClaim(
        memory_type=MemoryType.fact,
        canonical_key=canonical_key,
        content=content,
        evidence_quote=evidence,
        importance_score=0.9,
        confidence_score=0.95,
        sensitivity_hint=sensitivity,
    )


def test_feature_memory_and_history_control_plane(tmp_path: Path) -> None:
    app = create_app(stateful_settings(tmp_path / "private.sqlite3"))
    with TestClient(app) as client:
        runtime = app.state.memory_runtime
        assert isinstance(runtime, MemoryRuntime)

        features = client.get("/api/features")
        assert features.status_code == 200
        by_name = {item["name"]: item["enabled"] for item in features.json()}
        assert by_name["recent_history"] is True
        assert by_name["long_term_memory"] is False

        enabled = client.patch("/api/features/long_term_memory", json={"enabled": True})
        assert enabled.json() == {
            "name": "long_term_memory",
            "enabled": True,
            "disclosure": None,
        }

        with client.websocket_connect("/ws/client") as websocket:
            websocket.send_json({"type": "user.message", "payload": {"text": "请记住我喜欢红茶"}})
            while websocket.receive_json()["type"] != "assistant.completed":
                pass

        accepted = client.post("/api/chat", json={"text": "这一轮会被取消"}).json()
        cancelled = client.post(
            "/api/interrupt",
            json={"turn_id": accepted["turn_id"], "session_id": accepted["session_id"]},
        )
        assert cancelled.json()["status"] == "cancelled"

        source_text = "请记住我喜欢红茶"
        result = runtime.memory.consider_user_claim(
            claim(content="我喜欢红茶", evidence="我喜欢红茶", canonical_key="preference:tea"),
            user_id="local_user",
            source_message_id="manual-test-message",
            source_input_mode=SourceInputMode.text,
            source_text=source_text,
            created_at=datetime.now(UTC),
        )
        assert result.evaluation.decision is MemoryDecision.save
        assert result.item is not None
        memory_id = result.item.memory_id

        listed = client.get("/api/memory").json()
        assert [item["memory_id"] for item in listed] == [memory_id]
        assert (
            client.get("/api/memory/search", params={"query": "红茶"}).json()[0]["memory_id"]
            == memory_id
        )

        updated = client.patch(
            f"/api/memory/{memory_id}",
            json={"content": "我喜欢伯爵红茶"},
        )
        assert updated.status_code == 200
        assert updated.json()["content"] == "我喜欢伯爵红茶"
        forbidden = client.patch(
            f"/api/memory/{memory_id}",
            json={"content": "password: never-store-this"},
        )
        assert forbidden.status_code == 422
        assert "never-store-this" not in forbidden.text

        exported = client.get("/api/memory/export").json()
        assert exported["memories"][0]["memory_id"] == memory_id
        assert exported["sources"][memory_id]

        sensitive_text = "我的家庭地址是星海路 1 号"
        pending = runtime.memory.consider_user_claim(
            claim(
                content="住在星海路 1 号",
                evidence="家庭地址是星海路 1 号",
                canonical_key="profile:address",
                sensitivity=MemorySensitivity.address,
            ),
            user_id="local_user",
            source_message_id="sensitive-message",
            source_input_mode=SourceInputMode.text,
            source_text=sensitive_text,
            created_at=datetime.now(UTC),
        )
        assert pending.confirmation is not None
        confirmation_id = pending.confirmation.confirmation_id
        assert (
            client.get("/api/memory/confirmations").json()[0]["confirmation_id"] == confirmation_id
        )
        rejected = client.post(f"/api/memory/confirm/{confirmation_id}", json={"approved": False})
        assert rejected.json() == {"approved": False, "memory": None}
        assert (
            client.post("/api/memory/confirm/missing", json={"approved": True}).status_code == 404
        )

        assert client.delete(f"/api/memory/{memory_id}").json() == {"deleted": True}
        assert client.delete(f"/api/memory/{memory_id}").status_code == 404
        assert client.delete("/api/memory").json() == {"deleted": 0}

        cleared = client.post(
            "/api/history/clear",
            json={"user_id": "local_user", "session_id": "local_session"},
        )
        assert cleared.json()["deleted"] == 3

        disabled = client.patch("/api/features/long_term_memory", json={"enabled": False})
        assert disabled.json()["enabled"] is False


def test_private_state_api_is_explicitly_unavailable_when_storage_is_off() -> None:
    settings = Settings(logging=LoggingConfig(console_enabled=False, file_enabled=False))
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/features")
    assert response.status_code == 503
    assert response.json()["detail"] == "private_state_runtime_disabled"


def test_opt_in_candidate_analysis_uses_owned_provider_and_successful_user_turn_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CandidateLLM()
    monkeypatch.setattr("app.main.build_llm_provider", lambda _settings: provider)
    settings = stateful_settings(tmp_path / "candidate.sqlite3")
    settings.memory = settings.memory.model_copy(update={"candidate_analysis_enabled": True})

    with TestClient(create_app(settings)) as client:
        assert (
            client.patch("/api/features/long_term_memory", json={"enabled": True}).status_code
            == 200
        )
        with client.websocket_connect("/ws/client") as websocket:
            websocket.send_json({"type": "user.message", "payload": {"text": "我喜欢手冲咖啡"}})
            while websocket.receive_json()["type"] != "assistant.completed":
                pass

        memories = client.get("/api/memory").json()
        assert [item["content"] for item in memories] == ["用户喜欢手冲咖啡"]
        assert len(provider.requests) == 1

    assert provider.closed == 1
