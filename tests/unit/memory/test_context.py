from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.emotion.clock import FakeClock
from app.memory.context import MemoryContextAssembler
from app.memory.models import MemoryClaim, MemoryType, SourceInputMode
from app.memory.service import HistoryService, MemoryService
from app.schemas.ai import ChatRole, ContextOrigin, FeatureName
from app.storage.database import SQLiteDatabase
from app.storage.records import ConversationOrigin, ConversationRecord, ConversationRole
from app.storage.repositories import (
    ConversationRepository,
    FeatureFlagRepository,
    MemoryRepository,
)


def test_context_assembler_uses_shared_contracts_and_deduplicates_profiles(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)
    clock = FakeClock(now)
    database = SQLiteDatabase(tmp_path / "context.sqlite3")
    database.initialize()
    flags = FeatureFlagRepository(database)
    flags.set(FeatureName.long_term_memory, True, updated_at=now)
    history = HistoryService(ConversationRepository(database), flags, clock=clock)
    memory = MemoryService(MemoryRepository(database), flags, clock=clock)
    history.record(
        ConversationRecord(
            message_id="old-user",
            session_id="session",
            user_id="local_user",
            turn_id="turn-old",
            role=ConversationRole.user,
            origin=ConversationOrigin.user_text,
            content="以前的消息",
            created_at=now,
        )
    )
    history.record(
        ConversationRecord(
            message_id="old-assistant",
            session_id="session",
            user_id="local_user",
            turn_id="turn-old",
            role=ConversationRole.assistant,
            origin=ConversationOrigin.assistant_dialogue,
            content="以前的回复",
            created_at=now,
        )
    )
    history.record(
        ConversationRecord(
            message_id="current",
            session_id="session",
            user_id="local_user",
            turn_id="turn-current",
            role=ConversationRole.user,
            origin=ConversationOrigin.user_text,
            content="当前消息",
            created_at=now,
        )
    )
    source = "请记住我希望称呼为测试者"
    result = memory.consider_user_claim(
        MemoryClaim(
            memory_type=MemoryType.user_profile,
            canonical_key="profile:preferred_name",
            content="用户希望称呼为测试者",
            evidence_quote="我希望称呼为测试者",
            importance_score=0.9,
            confidence_score=0.95,
        ),
        user_id="local_user",
        source_message_id="current",
        source_input_mode=SourceInputMode.text,
        source_text=source,
        created_at=now,
    )
    assert result.item is not None
    emotion_source = "被鼓励时我通常会更有动力"
    emotion = memory.consider_user_claim(
        MemoryClaim(
            memory_type=MemoryType.emotion,
            canonical_key="emotion:encouragement_response",
            content=emotion_source,
            evidence_quote=emotion_source,
            importance_score=0.8,
            confidence_score=0.9,
        ),
        user_id="local_user",
        source_message_id="emotion-source",
        source_input_mode=SourceInputMode.text,
        source_text=emotion_source,
        created_at=now,
    )
    assert emotion.item is not None

    snapshot = MemoryContextAssembler(history, memory).build(
        user_id="local_user",
        session_id="session",
        query="",
        exclude_message_id="current",
    )

    assert [(item.message_id, item.role) for item in snapshot.history] == [
        ("old-assistant", ChatRole.assistant),
        ("old-user", ChatRole.user),
    ]
    assert len(snapshot.blocks) == 2
    assert snapshot.blocks[0].origin is ContextOrigin.user_profile
    assert snapshot.blocks[0].source_id == result.item.memory_id
    assert snapshot.blocks[0].persistable is False
    assert snapshot.blocks[1].origin is ContextOrigin.long_term_memory
    assert json.loads(snapshot.blocks[1].content)["usage"] == "tone_only"


def test_context_assembler_returns_no_memory_or_history_when_flags_are_disabled(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)
    database = SQLiteDatabase(tmp_path / "disabled.sqlite3")
    database.initialize()
    flags = FeatureFlagRepository(database)
    flags.set(FeatureName.recent_history, False, updated_at=now)
    history = HistoryService(ConversationRepository(database), flags, clock=FakeClock(now))
    memory = MemoryService(MemoryRepository(database), flags, clock=FakeClock(now))

    snapshot = MemoryContextAssembler(history, memory).build(
        user_id="local_user", session_id="session", query="anything"
    )

    assert snapshot.history == ()
    assert snapshot.blocks == ()
