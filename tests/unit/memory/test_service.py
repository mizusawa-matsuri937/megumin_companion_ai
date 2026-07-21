from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from app.emotion.clock import FakeClock
from app.memory.models import (
    ApprovedMemory,
    MemoryActionResult,
    MemoryClaim,
    MemoryDecision,
    MemoryEvaluation,
    MemoryItem,
    MemoryProposal,
    MemorySensitivity,
    MemoryType,
    ProfileItem,
    SourceInputMode,
)
from app.memory.policy import MemoryPolicy
from app.memory.service import (
    ConfirmationNotFoundError,
    CredentialRejectedError,
    FeatureDisabledError,
    HistoryService,
    MemoryService,
)
from app.schemas.ai import FeatureName, FeatureState
from app.storage.database import SQLiteDatabase
from app.storage.records import (
    ConversationOrigin,
    ConversationRecord,
    ConversationRole,
    DeletionResult,
)
from app.storage.repositories import (
    ConversationRepository,
    FeatureFlagRepository,
    MemoryRepository,
)


def _now() -> datetime:
    return datetime(2026, 7, 13, 12, tzinfo=UTC)


def _database(tmp_path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(tmp_path / "companion.sqlite3")
    database.initialize()
    return database


def _claim(source: str = "我喜欢手冲咖啡") -> MemoryClaim:
    return MemoryClaim(
        memory_type=MemoryType.fact,
        canonical_key="preference:coffee",
        content=source,
        evidence_quote=source,
        importance_score=0.8,
        confidence_score=0.9,
    )


def _consider(service: MemoryService, source: str = "我喜欢手冲咖啡") -> MemoryActionResult:
    return service.consider_user_claim(
        _claim(source),
        user_id="local_user",
        source_message_id="msg-1",
        source_input_mode=SourceInputMode.text,
        source_text=source,
        created_at=_now(),
    )


def test_disabled_long_term_memory_performs_no_runtime_memory_store_access() -> None:
    store = CountingMemoryStore()
    flags = InMemoryFlags(long_term_memory=False)
    service = MemoryService(store, flags, clock=FakeClock(_now()))

    result = _consider(service)
    assert result.evaluation.reason_code == "long_term_memory_disabled"
    assert service.retrieve_for_context(user_id="local_user", query="咖啡") == []
    assert service.profiles_for_context(user_id="local_user") == []
    assert store.calls == []


def test_enabled_ordinary_claim_is_saved_and_retrievable(tmp_path: Path) -> None:
    database = _database(tmp_path)
    flags = FeatureFlagRepository(database)
    flags.set(FeatureName.long_term_memory, True, updated_at=_now())
    service = MemoryService(MemoryRepository(database), flags, clock=FakeClock(_now()))

    result = _consider(service)

    assert result.evaluation.decision is MemoryDecision.save
    assert result.item is not None
    assert [
        item.memory_id
        for item in service.retrieve_for_context(user_id="local_user", query="手冲咖啡")
    ] == [result.item.memory_id]


def test_sensitive_confirmation_is_memory_only_and_expires_after_fifteen_minutes(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    flags = FeatureFlagRepository(database)
    flags.set(FeatureName.long_term_memory, True, updated_at=_now())
    clock = FakeClock(_now())
    repository = MemoryRepository(database)
    service = MemoryService(repository, flags, clock=clock)
    source = "我被诊断为测试性焦虑症"

    result = _consider(service, source)
    assert result.confirmation is not None
    assert repository.list_items(user_id="local_user") == []
    confirmation_id = result.confirmation.confirmation_id

    clock.advance(timedelta(minutes=15))
    assert service.prune_expired_confirmations() == 1
    assert service.pending_confirmations() == ()
    with pytest.raises(ConfirmationNotFoundError):
        service.confirm(confirmation_id, approved=True)


def test_sensitive_confirmation_can_be_approved_or_rejected(tmp_path: Path) -> None:
    database = _database(tmp_path)
    flags = FeatureFlagRepository(database)
    flags.set(FeatureName.long_term_memory, True, updated_at=_now())
    repository = MemoryRepository(database)
    service = MemoryService(repository, flags, clock=FakeClock(_now()))

    rejected = _consider(service, "我的家庭地址是测试路一号")
    assert rejected.confirmation is not None
    assert service.confirm(rejected.confirmation.confirmation_id, approved=False) is None
    assert repository.list_items(user_id="local_user") == []

    accepted = _consider(service, "我的家庭地址是测试路二号")
    assert accepted.confirmation is not None
    item = service.confirm(accepted.confirmation.confirmation_id, approved=True)
    assert item is not None
    assert item.sensitivity is MemorySensitivity.address


def test_disabling_memory_clears_pending_and_blocks_stale_confirmation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    flags = FeatureFlagRepository(database)
    flags.set(FeatureName.long_term_memory, True, updated_at=_now())
    service = MemoryService(MemoryRepository(database), flags, clock=FakeClock(_now()))
    result = _consider(service, "我的家庭地址是测试路一号")
    assert result.confirmation is not None

    state = service.set_enabled(False)

    assert not state.enabled
    assert service.pending_confirmations() == ()
    with pytest.raises(ConfirmationNotFoundError):
        service.confirm(result.confirmation.confirmation_id, approved=True)


def test_external_flag_disable_is_rechecked_before_confirmation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    flags = FeatureFlagRepository(database)
    flags.set(FeatureName.long_term_memory, True, updated_at=_now())
    service = MemoryService(MemoryRepository(database), flags, clock=FakeClock(_now()))
    result = _consider(service, "我的家庭地址是测试路一号")
    assert result.confirmation is not None
    flags.set(FeatureName.long_term_memory, False, updated_at=_now())

    with pytest.raises(FeatureDisabledError):
        service.confirm(result.confirmation.confirmation_id, approved=True)


def test_management_edit_rejects_credentials_and_is_allowed_while_disabled(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    flags = FeatureFlagRepository(database)
    flags.set(FeatureName.long_term_memory, True, updated_at=_now())
    repository = MemoryRepository(database)
    service = MemoryService(repository, flags, clock=FakeClock(_now()))
    result = _consider(service)
    assert result.item is not None
    service.set_enabled(False)

    updated = service.update_for_management(
        result.item.memory_id,
        user_id="local_user",
        content="用户喜欢浅烘咖啡",
    )
    assert updated is not None
    assert updated.content == "用户喜欢浅烘咖啡"
    assert service.list_for_management(user_id="local_user") == [updated]
    with pytest.raises(ValueError, match="blank"):
        service.update_for_management(result.item.memory_id, user_id="local_user", content="   ")
    with pytest.raises(CredentialRejectedError):
        service.update_for_management(
            result.item.memory_id,
            user_id="local_user",
            content="password = fake-password-12345",
        )
    assert service.delete_for_management(result.item.memory_id, user_id="local_user")
    assert service.clear_for_management(user_id="local_user") == 0


def test_disabled_history_is_strict_runtime_noop_but_management_clear_remains_available() -> None:
    store = CountingConversationStore()
    flags = InMemoryFlags(recent_history=False)
    service = HistoryService(store, flags, clock=FakeClock(_now()))
    record = ConversationRecord(
        message_id="msg",
        session_id="session",
        user_id="local_user",
        turn_id="turn",
        role=ConversationRole.user,
        origin=ConversationOrigin.user_text,
        content="explicit message",
        created_at=_now(),
    )

    assert not service.record(record)
    assert service.recent(user_id="local_user", session_id="session") == []
    assert service.cleanup() == 0
    assert store.calls == ["cleanup_expired"]
    assert service.clear_for_management(user_id="local_user") == 0
    assert store.calls == ["cleanup_expired", "clear"]


def test_history_service_uses_seven_day_repository_semantics(tmp_path: Path) -> None:
    database = _database(tmp_path)
    clock = FakeClock(_now())
    service = HistoryService(
        ConversationRepository(database), FeatureFlagRepository(database), clock=clock
    )
    record = ConversationRecord(
        message_id="msg",
        session_id="session",
        user_id="local_user",
        turn_id="turn",
        role=ConversationRole.user,
        origin=ConversationOrigin.user_voice,
        content="voice transcript",
        created_at=clock.now(),
    )

    assert service.record(record)
    assert service.recent(user_id="local_user", session_id="session") == [record]
    clock.advance(timedelta(days=7, microseconds=1))
    assert service.cleanup() == 1


def test_history_disable_waits_for_inflight_write_and_blocks_all_later_writes() -> None:
    store = BlockingConversationStore()
    flags = InMemoryFlags(recent_history=True)
    service = HistoryService(store, flags, clock=FakeClock(_now()))
    record = ConversationRecord(
        message_id="race",
        session_id="session",
        user_id="local_user",
        turn_id="turn",
        role=ConversationRole.user,
        origin=ConversationOrigin.user_text,
        content="linearized history write",
        created_at=_now(),
    )
    disable_started = Event()

    def disable() -> FeatureState:
        disable_started.set()
        return service.set_enabled(False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        record_future = executor.submit(service.record, record)
        assert store.entered.wait(timeout=1)
        disable_future = executor.submit(disable)
        assert disable_started.wait(timeout=1)
        assert not disable_future.done()
        store.release.set()
        assert record_future.result(timeout=1)
        assert not disable_future.result(timeout=1).enabled

    assert not service.record(record.model_copy(update={"message_id": "later"}))
    assert store.calls == ["add"]


def test_memory_disable_serializes_with_candidate_and_clears_late_confirmation() -> None:
    policy = BlockingMemoryPolicy()
    service = MemoryService(
        CountingMemoryStore(),
        InMemoryFlags(long_term_memory=True),
        policy=policy,
        clock=FakeClock(_now()),
    )
    disable_started = Event()

    def disable() -> FeatureState:
        disable_started.set()
        return service.set_enabled(False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        candidate = executor.submit(_consider, service, "我的家庭地址是测试路一号")
        assert policy.entered.wait(timeout=1)
        disabled = executor.submit(disable)
        assert disable_started.wait(timeout=1)
        assert not disabled.done()
        policy.release.set()
        assert candidate.result(timeout=1).confirmation is not None
        assert not disabled.result(timeout=1).enabled

    assert service.pending_confirmations() == ()


def test_service_configuration_rejects_nonpositive_time_windows() -> None:
    flags = InMemoryFlags()
    with pytest.raises(ValueError, match="retention_days"):
        HistoryService(CountingConversationStore(), flags, retention_days=0)
    with pytest.raises(ValueError, match="confirmation_ttl"):
        MemoryService(
            CountingMemoryStore(),
            flags,
            confirmation_ttl=timedelta(0),
        )


class InMemoryFlags:
    def __init__(self, *, recent_history: bool = True, long_term_memory: bool = False) -> None:
        self._states = {
            FeatureName.recent_history: recent_history,
            FeatureName.long_term_memory: long_term_memory,
        }

    def get(self, name: FeatureName) -> FeatureState:
        return FeatureState(name=name, enabled=self._states.get(name, False))

    def set(self, name: FeatureName, enabled: bool, *, updated_at: datetime) -> FeatureState:
        del updated_at
        self._states[name] = enabled
        return FeatureState(name=name, enabled=enabled)


class CountingMemoryStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def upsert(self, approved: ApprovedMemory) -> MemoryItem:
        del approved
        self.calls.append("upsert")
        raise AssertionError("disabled service touched memory store")

    def search(self, *, user_id: str, query: str, limit: int = 10) -> list[MemoryItem]:
        del user_id, query, limit
        self.calls.append("search")
        raise AssertionError("disabled service touched memory store")

    def get(self, memory_id: str, *, user_id: str | None = None) -> MemoryItem | None:
        del memory_id, user_id
        self.calls.append("get")
        raise AssertionError("unexpected management call")

    def list_items(
        self,
        *,
        user_id: str,
        include_superseded: bool = False,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        del user_id, include_superseded, limit
        self.calls.append("list_items")
        raise AssertionError("unexpected management call")

    def list_profiles(self, *, user_id: str) -> list[ProfileItem]:
        del user_id
        self.calls.append("list_profiles")
        raise AssertionError("disabled service touched memory store")

    def update_content(
        self,
        memory_id: str,
        *,
        user_id: str,
        content: str,
        normalized_content: str,
        updated_at: datetime,
    ) -> MemoryItem | None:
        del memory_id, user_id, content, normalized_content, updated_at
        self.calls.append("update_content")
        raise AssertionError("unexpected management call")

    def delete(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        now: datetime,
    ) -> bool:
        del memory_id, user_id, now
        self.calls.append("delete")
        raise AssertionError("unexpected management call")

    def delete_logically(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        now: datetime,
    ) -> DeletionResult:
        del memory_id, user_id, now
        self.calls.append("delete_logically")
        raise AssertionError("unexpected management call")

    def clear(self, *, user_id: str, now: datetime) -> int:
        del user_id, now
        self.calls.append("clear")
        return 0

    def clear_logically(self, *, user_id: str, now: datetime) -> DeletionResult:
        del user_id, now
        self.calls.append("clear_logically")
        return DeletionResult(deleted_count=0)


class CountingConversationStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def add(self, record: ConversationRecord) -> bool:
        del record
        self.calls.append("add")
        raise AssertionError("disabled service touched history store")

    def list_recent(
        self,
        *,
        user_id: str,
        session_id: str,
        now: datetime,
        retention_days: int = 7,
        limit: int = 100,
        exclude_message_id: str | None = None,
    ) -> list[ConversationRecord]:
        del user_id, session_id, now, retention_days, limit, exclude_message_id
        self.calls.append("list_recent")
        raise AssertionError("disabled service touched history store")

    def cleanup_expired(self, *, now: datetime, retention_days: int = 7) -> int:
        del now, retention_days
        self.calls.append("cleanup_expired")
        return 0

    def clear(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        now: datetime,
    ) -> int:
        del user_id, session_id, now
        self.calls.append("clear")
        return 0

    def clear_logically(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        now: datetime,
    ) -> DeletionResult:
        del user_id, session_id, now
        self.calls.append("clear_logically")
        return DeletionResult(deleted_count=0)


class BlockingConversationStore(CountingConversationStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def add(self, record: ConversationRecord) -> bool:
        del record
        self.calls.append("add")
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test did not release history store")
        return True


class BlockingMemoryPolicy(MemoryPolicy):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def evaluate(self, proposal: MemoryProposal) -> MemoryEvaluation:
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test did not release memory policy")
        return super().evaluate(proposal)
