from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.memory.models import (
    ApprovedMemory,
    MemorySensitivity,
    MemorySourceKind,
    MemoryStatus,
    MemoryType,
    SourceProvenance,
)
from app.schemas.ai import FeatureName
from app.storage.database import SQLiteDatabase, StorageConflictError
from app.storage.records import ConversationOrigin, ConversationRecord, ConversationRole
from app.storage.repositories import (
    ConversationRepository,
    FeatureFlagRepository,
    MemoryRepository,
)
from pydantic import ValidationError


def _now() -> datetime:
    return datetime(2026, 7, 13, 12, tzinfo=UTC)


def _database(tmp_path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(tmp_path / "companion.sqlite3")
    database.initialize()
    return database


def _conversation(
    message_id: str,
    created_at: datetime,
    *,
    turn_id: str | None = None,
    content: str | None = None,
) -> ConversationRecord:
    return ConversationRecord(
        message_id=message_id,
        session_id="session-1",
        user_id="local_user",
        turn_id=turn_id or f"turn-{message_id}",
        role=ConversationRole.user,
        origin=ConversationOrigin.user_text,
        content=content or message_id,
        created_at=created_at,
    )


def _approved(
    *,
    memory_id: str,
    content: str,
    normalized: str,
    source_message_id: str,
    created_at: datetime | None = None,
    memory_type: MemoryType = MemoryType.fact,
    canonical_key: str = "preference:drink",
) -> ApprovedMemory:
    return ApprovedMemory(
        memory_id=memory_id,
        user_id="local_user",
        memory_type=memory_type,
        canonical_key=canonical_key,
        content=content,
        normalized_content=normalized,
        importance_score=0.8,
        confidence_score=0.9,
        sensitivity=MemorySensitivity.normal,
        source_kind=MemorySourceKind.dialogue,
        source_message_id=source_message_id,
        evidence_quote=f"source {source_message_id}: {content}",
        source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        provenance=SourceProvenance.dialogue_text,
        created_at=created_at or _now(),
    )


def test_recent_history_retention_boundary_order_idempotency_and_cleanup(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = ConversationRepository(database)
    now = _now()
    old = _conversation("old", now - timedelta(days=7, microseconds=1))
    boundary = _conversation("boundary", now - timedelta(days=7))
    recent = _conversation("recent", now)

    assert repository.add(old)
    assert repository.add(boundary)
    assert repository.add(recent)
    assert not repository.add(recent)
    assert [
        item.message_id
        for item in repository.list_recent(
            user_id="local_user", session_id="session-1", now=now, exclude_message_id="recent"
        )
    ] == ["boundary"]

    assert repository.cleanup_expired(now=now) == 1
    assert repository.cleanup_expired(now=now) == 0
    assert [
        item.message_id
        for item in repository.list_recent(user_id="local_user", session_id="session-1", now=now)
    ] == ["boundary", "recent"]


def test_history_rejects_reused_message_or_turn_role(tmp_path: Path) -> None:
    repository = ConversationRepository(_database(tmp_path))
    first = _conversation("same", _now(), turn_id="turn-1", content="first")
    repository.add(first)

    with pytest.raises(StorageConflictError):
        repository.add(_conversation("same", _now(), turn_id="turn-2", content="changed"))
    with pytest.raises(StorageConflictError):
        repository.add(_conversation("different", _now(), turn_id="turn-1"))


def test_history_validates_arguments_and_clears_only_selected_session(tmp_path: Path) -> None:
    repository = ConversationRepository(_database(tmp_path))
    repository.add(_conversation("first", _now()))
    other = _conversation("other", _now()).model_copy(
        update={"session_id": "session-2", "turn_id": "turn-other"}
    )
    repository.add(other)

    with pytest.raises(ValueError, match="positive"):
        repository.list_recent(user_id="local_user", session_id="session-1", now=_now(), limit=0)
    with pytest.raises(ValueError, match="positive"):
        repository.cleanup_expired(now=_now(), retention_days=0)
    assert repository.clear(user_id="local_user", session_id="session-1", now=_now()) == 1
    assert repository.list_recent(user_id="local_user", session_id="session-2", now=_now()) == [
        other
    ]
    assert repository.clear(user_id="local_user", now=_now()) == 1


def test_feature_flags_have_privacy_safe_defaults_and_persist_updates(tmp_path: Path) -> None:
    repository = FeatureFlagRepository(_database(tmp_path))
    states = {state.name: state.enabled for state in repository.list()}

    assert states == {
        FeatureName.recent_history: True,
        FeatureName.long_term_memory: False,
        FeatureName.vision: False,
        FeatureName.cloud_vision: False,
        FeatureName.proactive: False,
    }
    changed = repository.set(FeatureName.long_term_memory, True, updated_at=_now())
    assert changed.enabled
    assert repository.get(FeatureName.long_term_memory).enabled


def test_missing_feature_flag_is_reported_as_database_corruption(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = FeatureFlagRepository(database)
    with database.connect() as connection:
        connection.execute("DELETE FROM feature_flags WHERE name = 'vision'")

    with pytest.raises(KeyError, match="vision"):
        repository.get(FeatureName.vision)
    with pytest.raises(KeyError, match="vision"):
        repository.set(FeatureName.vision, True, updated_at=_now())


def test_memory_exact_dedup_merges_sources_without_growing_items(tmp_path: Path) -> None:
    repository = MemoryRepository(_database(tmp_path))
    first = repository.upsert(
        _approved(
            memory_id="mem-1",
            content="用户喜欢手冲咖啡",
            normalized="用户喜欢手冲咖啡",
            source_message_id="msg-1",
        )
    )
    second = repository.upsert(
        _approved(
            memory_id="mem-2",
            content="用户喜欢手冲咖啡",
            normalized="用户喜欢手冲咖啡",
            source_message_id="msg-2",
            created_at=_now() + timedelta(minutes=1),
        )
    )

    assert second.memory_id == first.memory_id
    assert len(repository.list_items(user_id="local_user", include_superseded=True)) == 1
    assert {source.source_message_id for source in repository.list_sources(first.memory_id)} == {
        "msg-1",
        "msg-2",
    }


def test_conflicting_slot_supersedes_old_value_and_updates_profile_projection(
    tmp_path: Path,
) -> None:
    repository = MemoryRepository(_database(tmp_path))
    old = repository.upsert(
        _approved(
            memory_id="mem-old",
            content="用户希望称呼为小夜",
            normalized="用户希望称呼为小夜",
            source_message_id="msg-old",
            memory_type=MemoryType.user_profile,
            canonical_key="profile:preferred_name",
        )
    )
    new = repository.upsert(
        _approved(
            memory_id="mem-new",
            content="用户希望称呼为阿夜",
            normalized="用户希望称呼为阿夜",
            source_message_id="msg-new",
            memory_type=MemoryType.user_profile,
            canonical_key="profile:preferred_name",
            created_at=_now() + timedelta(days=1),
        )
    )

    items = repository.list_items(user_id="local_user", include_superseded=True)
    assert {item.memory_id: item.status for item in items} == {
        old.memory_id: MemoryStatus.superseded,
        new.memory_id: MemoryStatus.active,
    }
    profiles = repository.list_profiles(user_id="local_user")
    assert [(item.memory_id, item.value) for item in profiles] == [
        (new.memory_id, "用户希望称呼为阿夜")
    ]


def test_chinese_fts_and_short_like_search_only_return_active_items(tmp_path: Path) -> None:
    repository = MemoryRepository(_database(tmp_path))
    repository.upsert(
        _approved(
            memory_id="mem-coffee",
            content="用户喜欢手冲咖啡",
            normalized="用户喜欢手冲咖啡",
            source_message_id="msg-coffee",
        )
    )

    assert [
        item.memory_id for item in repository.search(user_id="local_user", query="手冲咖啡")
    ] == ["mem-coffee"]
    assert [item.memory_id for item in repository.search(user_id="local_user", query="咖啡")] == [
        "mem-coffee"
    ]
    assert repository.search(user_id="local_user", query='" OR *') == []
    assert repository.search(user_id="local_user", query="", limit=1)[0].memory_id == "mem-coffee"
    with pytest.raises(ValueError, match="limit"):
        repository.search(user_id="local_user", query="coffee", limit=0)
    assert repository.get("mem-coffee", user_id="other_user") is None


def test_hard_delete_cascades_sources_profile_and_fts_then_truncates_wal(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = MemoryRepository(database)
    item = repository.upsert(
        _approved(
            memory_id="mem-delete",
            content="用户名字是测试者",
            normalized="用户名字是测试者",
            source_message_id="msg-delete",
            memory_type=MemoryType.user_profile,
            canonical_key="profile:name",
        )
    )

    assert repository.delete(item.memory_id, user_id="local_user", now=_now())
    assert not repository.delete(item.memory_id, user_id="local_user", now=_now())
    assert repository.get(item.memory_id) is None
    assert repository.list_sources(item.memory_id) == []
    assert repository.list_profiles(user_id="local_user") == []
    assert repository.search(user_id="local_user", query="测试者") == []
    wal = Path(f"{database.path}-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_manual_update_reindexes_adds_source_and_updates_profile(tmp_path: Path) -> None:
    repository = MemoryRepository(_database(tmp_path))
    item = repository.upsert(
        _approved(
            memory_id="mem-edit",
            content="用户名字是测试甲",
            normalized="用户名字是测试甲",
            source_message_id="msg-edit",
            memory_type=MemoryType.user_profile,
            canonical_key="profile:name",
        )
    )

    updated = repository.update_content(
        item.memory_id,
        user_id="local_user",
        content="用户名字是测试乙",
        normalized_content="用户名字是测试乙",
        updated_at=_now() + timedelta(minutes=1),
    )

    assert updated is not None
    assert updated.content == "用户名字是测试乙"
    assert updated.confidence_score == 1.0
    assert updated.source_kind is MemorySourceKind.manual
    assert len(repository.list_sources(item.memory_id)) == 2
    assert repository.search(user_id="local_user", query="测试甲") == []
    assert [
        found.memory_id for found in repository.search(user_id="local_user", query="测试乙")
    ] == [item.memory_id]
    assert repository.list_profiles(user_id="local_user")[0].value == "用户名字是测试乙"
    assert (
        repository.update_content(
            "missing",
            user_id="local_user",
            content="nothing",
            normalized_content="nothing",
            updated_at=_now(),
        )
        is None
    )


def test_repository_defense_rejects_credential_edits_and_duplicate_values(
    tmp_path: Path,
) -> None:
    repository = MemoryRepository(_database(tmp_path))
    old = repository.upsert(
        _approved(
            memory_id="old",
            content="旧值内容",
            normalized="旧值内容",
            source_message_id="old-message",
        )
    )
    new = repository.upsert(
        _approved(
            memory_id="new",
            content="新值内容",
            normalized="新值内容",
            source_message_id="new-message",
            created_at=_now() + timedelta(minutes=1),
        )
    )

    with pytest.raises(StorageConflictError, match="duplicates"):
        repository.update_content(
            new.memory_id,
            user_id="local_user",
            content=old.content,
            normalized_content=old.normalized_content,
            updated_at=_now() + timedelta(minutes=2),
        )
    with pytest.raises(ValueError, match="credential"):
        repository.update_content(
            new.memory_id,
            user_id="local_user",
            content="password = fake-password-12345",
            normalized_content="password = fake-password-12345",
            updated_at=_now() + timedelta(minutes=2),
        )


def test_exact_key_with_changed_type_is_rejected_atomically(tmp_path: Path) -> None:
    repository = MemoryRepository(_database(tmp_path))
    original = _approved(
        memory_id="fact",
        content="相同内容",
        normalized="相同内容",
        source_message_id="fact-message",
    )
    repository.upsert(original)
    changed_type = original.model_copy(
        update={"memory_id": "relationship", "memory_type": MemoryType.relationship}
    )

    with pytest.raises(StorageConflictError, match="changed type"):
        repository.upsert(changed_type)
    assert repository.get("fact", user_id="local_user") is not None
    assert repository.get("relationship") is None


def test_clear_removes_only_selected_users_memories(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = MemoryRepository(database)
    repository.upsert(
        _approved(
            memory_id="local",
            content="本地用户事实",
            normalized="本地用户事实",
            source_message_id="local-message",
        )
    )
    other = _approved(
        memory_id="other",
        content="其他用户事实",
        normalized="其他用户事实",
        source_message_id="other-message",
    ).model_copy(update={"user_id": "other_user"})
    repository.upsert(other)

    assert repository.clear(user_id="local_user", now=_now()) == 1
    assert repository.list_items(user_id="local_user") == []
    assert [item.memory_id for item in repository.list_items(user_id="other_user")] == ["other"]
    assert repository.clear(user_id="local_user", now=_now()) == 0


def test_conversation_record_rejects_invalid_role_time_and_blank_content() -> None:
    payload = {
        "message_id": "message",
        "session_id": "session",
        "user_id": "user",
        "turn_id": "turn",
        "role": "assistant",
        "origin": "user_text",
        "content": "text",
        "created_at": _now(),
    }
    with pytest.raises(ValidationError, match="role"):
        ConversationRecord.model_validate(payload)
    payload.update(role="user", created_at=datetime(2026, 7, 13), content="   ")
    with pytest.raises(ValidationError):
        ConversationRecord.model_validate(payload)
