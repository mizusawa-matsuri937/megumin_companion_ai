"""Private-state API, turn persistence, confirmation, and deletion integration."""

import hashlib
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.api.security import DevAPIConfig, DevAPIScope
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
from app.paths import AppPaths
from app.proactive import ProactiveRuntime
from app.schemas import ChatCompletion, ChatRequest
from app.storage import SQLiteDatabase
from app.storage.database import MIGRATIONS, apply_migrations, transaction
from fastapi import FastAPI
from fastapi.testclient import TestClient

DEV_API = DevAPIConfig(
    token="integration-memory-token-000000000000000000000",
    client_id="client_integration_memory",
    session_id="local_session",
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
        client=("127.0.0.1", 51002),
    )


def command(command_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "command_id": f"command-{command_type}-{id(payload)}",
        "client_id": DEV_API.client_id,
        "type": command_type,
        "session_id": DEV_API.session_id,
        "payload": payload,
    }


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
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        llm=LLMConfig(provider="mock"),
        storage=StorageConfig(enabled=True, database_path=Path(database_path.name)),
        memory=MemoryConfig(history_retention_days=7, confirmation_ttl_minutes=15),
        pipeline=PipelineConfig(mock_token_delay_ms=10, mock_audio_duration_ms=0),
    )
    settings._paths = AppPaths(root=database_path.parent)
    return settings


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
    app = secured_app(stateful_settings(tmp_path / "private.sqlite3"))
    with secured_client(app) as client:
        runtime = app.state.memory_runtime
        assert isinstance(runtime, MemoryRuntime)
        assert isinstance(app.state.proactive_runtime, ProactiveRuntime)
        assert not app.state.proactive_runtime.snapshot().scheduler_active

        features = client.get("/api/features")
        assert features.status_code == 200
        by_name = {item["name"]: item["enabled"] for item in features.json()}
        assert by_name["recent_history"] is True
        assert by_name["long_term_memory"] is False
        storage = client.get("/api/storage/status")
        assert storage.status_code == 200
        assert storage.json()["mode"] == "read_write"
        assert storage.json()["schema_version"] == 3
        assert storage.json()["reason_code"] is None
        assert storage.json()["backups"] == []
        assert (
            client.post(
                "/api/storage/recovery/restore",
                json={"backup_name": "missing.backup", "backup_sha256": "0" * 64},
            ).json()["detail"]
            == "database_not_in_safe_mode"
        )
        assert (
            client.post("/api/storage/recovery/retry").json()["detail"]
            == "database_not_in_safe_mode"
        )

        proactive_enabled = client.patch("/api/features/proactive", json={"enabled": True})
        assert proactive_enabled.status_code == 200
        assert app.state.proactive_runtime.snapshot().scheduler_active
        proactive_disabled = client.patch("/api/features/proactive", json={"enabled": False})
        assert proactive_disabled.status_code == 200
        assert not app.state.proactive_runtime.snapshot().scheduler_active

        enabled = client.patch("/api/features/long_term_memory", json={"enabled": True})
        enabled_state = enabled.json()
        assert enabled_state["name"] == "long_term_memory"
        assert enabled_state["desired_state"] == "enabled"
        assert enabled_state["actual_state"] == "enabled"
        assert enabled_state["generation"] == 1
        assert enabled_state["enabled"] is True

        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_json(command("session.resume", {"last_seq": 0}))
            websocket.send_json(command("user.message", {"text": "请记住我喜欢红茶"}))
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

        deletion = client.delete(f"/api/memory/{memory_id}").json()
        assert deletion["deleted"] is True
        assert deletion["logical_deleted"] is True
        assert deletion["cleanup_pending"] is True
        assert deletion["cleanup_id"].startswith("cleanup_")
        cleanup = client.get(f"/api/storage/cleanup/{deletion['cleanup_id']}")
        assert cleanup.status_code == 200
        assert cleanup.json()["state"] == "pending"
        assert cleanup.json()["reason_code"] is None
        assert client.get("/api/storage/cleanup/missing-cleanup").status_code == 404
        assert client.delete(f"/api/memory/{memory_id}").status_code == 404
        assert client.delete("/api/memory").json()["deleted"] == 0

        cleared = client.post(
            "/api/history/clear",
            json={"user_id": "local_user", "session_id": "local_session"},
        )
        assert cleared.json()["deleted"] == 3
        assert cleared.json()["logical_deleted"] is True
        assert cleared.json()["cleanup_pending"] is True

        disabled = client.patch("/api/features/long_term_memory", json={"enabled": False})
        assert disabled.json()["enabled"] is False


def test_private_state_api_is_explicitly_unavailable_when_storage_is_off(
    tmp_path: Path,
) -> None:
    settings = Settings(logging=LoggingConfig(console_enabled=False, file_enabled=False))
    settings._paths = AppPaths(root=tmp_path / "app")
    app = secured_app(settings)
    with secured_client(app) as client:
        response = client.get("/api/features")
    assert response.status_code == 503
    assert response.json()["detail"] == "private_state_runtime_disabled"


def test_future_schema_starts_content_free_safe_mode_with_recovery_status(
    tmp_path: Path,
) -> None:
    settings = stateful_settings(tmp_path / "future.sqlite3")
    database_path = settings.database_path()
    database = SQLiteDatabase(database_path)
    with database.connect() as connection:
        assert apply_migrations(connection, migrations=MIGRATIONS[:2]) == 2
    assert database.initialize() == 3
    backup = next(database_path.parent.glob("future.sqlite3.pre-v2-to-v3.backup"))
    backup_sha256 = hashlib.sha256(backup.read_bytes()).hexdigest()
    with database.connect() as connection, transaction(connection):
        connection.execute("INSERT INTO schema_migrations VALUES (99, 'synthetic_future', 'now')")

    app = secured_app(settings)
    with secured_client(app) as client:
        status = client.get("/api/storage/status")
        assert status.status_code == 200
        payload = status.json()
        assert payload["mode"] == "safe_read_only"
        assert payload["schema_version"] == 99
        assert payload["reason_code"] == "db_future_schema"
        assert payload["recovery_options"] == ["restore_backup", "keep_read_only"]
        assert payload["backups"] == [{"name": backup.name, "sha256": backup_sha256}]
        features = client.get("/api/features")
        assert features.status_code == 503
        assert features.json()["detail"] == "database_safe_mode"
        capabilities = client.get("/health/capabilities")
        memory = next(
            item for item in capabilities.json()["capabilities"] if item["name"] == "memory"
        )
        assert memory == {
            "name": "memory",
            "status": "unavailable",
            "error_code": "db_future_schema",
        }
        retry = client.post("/api/storage/recovery/retry")
        assert retry.status_code == 409
        assert retry.json()["detail"] == "database_retry_not_available"
        invalid_restore = client.post(
            "/api/storage/recovery/restore",
            json={"backup_name": backup.name, "backup_sha256": "0" * 64},
        )
        assert invalid_restore.status_code == 409
        assert invalid_restore.json()["detail"] == "database_restore_failed"
        restored = client.post(
            "/api/storage/recovery/restore",
            json={"backup_name": backup.name, "backup_sha256": backup_sha256},
        )
        assert restored.status_code == 200
        assert restored.json() == {"restored_schema_version": 2, "restart_required": True}


def test_opt_in_candidate_analysis_uses_owned_provider_and_successful_user_turn_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CandidateLLM()
    monkeypatch.setattr("app.main.build_llm_provider", lambda _settings: provider)
    settings = stateful_settings(tmp_path / "candidate.sqlite3")
    settings.memory = settings.memory.model_copy(update={"candidate_analysis_enabled": True})

    with secured_client(secured_app(settings)) as client:
        assert (
            client.patch("/api/features/long_term_memory", json={"enabled": True}).status_code
            == 200
        )
        with client.websocket_connect(f"{WS_BASE_URL}/ws/client") as websocket:
            websocket.send_json(command("session.resume", {"last_seq": 0}))
            websocket.send_json(command("user.message", {"text": "我喜欢手冲咖啡"}))
            while websocket.receive_json()["type"] != "assistant.completed":
                pass

        memories: list[dict[str, object]] = []
        for _ in range(100):
            memories = client.get("/api/memory").json()
            if memories:
                break
            time.sleep(0.01)
        assert [item["content"] for item in memories] == ["用户喜欢手冲咖啡"]
        assert len(provider.requests) == 1

    assert provider.closed == 1


def test_application_shutdown_attempts_memory_after_turn_service_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = secured_app(stateful_settings(tmp_path / "shutdown.sqlite3"))
    memory_closed = 0

    with secured_client(app) as client:
        assert client.get("/health").status_code == 200
        runtime = app.state.memory_runtime
        service = app.state.turn_service
        assert isinstance(runtime, MemoryRuntime)
        assert service is not None
        original_memory_close = runtime.close

        async def failing_service_close() -> None:
            raise RuntimeError("synthetic private shutdown failure")

        async def recording_memory_close() -> None:
            nonlocal memory_closed
            memory_closed += 1
            await original_memory_close()

        monkeypatch.setattr(service, "shutdown", failing_service_close)
        monkeypatch.setattr(runtime, "close", recording_memory_close)

    assert memory_closed == 1
