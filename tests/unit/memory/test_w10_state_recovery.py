from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import app.storage.repositories as repositories_module
import pytest
from app.emotion import FakeClock
from app.memory import (
    MemoryClaim,
    MemorySensitivity,
    MemoryType,
    SourceInputMode,
)
from app.memory.runtime import (
    MemoryRuntime,
    SafeModeMemoryRuntime,
    _maintenance_error_code,
    create_memory_runtime,
)
from app.schemas import FeatureActualState, FeatureName, FeatureState
from app.storage import (
    ConversationOrigin,
    ConversationRecord,
    ConversationRole,
    DatabaseAccessMode,
    DatabaseStatus,
    MigrationError,
    SQLiteDatabase,
)
from app.storage.database import transaction

_NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)


class _CloseRecordingAnalyzer:
    def __init__(self) -> None:
        self.close_calls = 0

    async def analyze(self, *_args: object) -> list[MemoryClaim]:
        return []

    async def close(self) -> None:
        self.close_calls += 1


async def _runtime(path: Path, clock: FakeClock) -> MemoryRuntime:
    runtime = await create_memory_runtime(str(path), clock=clock)
    assert isinstance(runtime, MemoryRuntime)
    return runtime


async def _restart_runtime_after_single_busy_retry(
    path: Path,
    clock: FakeClock,
) -> MemoryRuntime:
    candidate = await create_memory_runtime(str(path), clock=clock)
    if isinstance(candidate, MemoryRuntime):
        assert candidate._clock is clock
        return candidate

    recovery = await candidate.recovery_status()
    reason = recovery["reason_code"]
    if reason != "db_busy":
        await candidate.close()
        pytest.fail(f"unexpected restart safe mode: {reason}")
    if "retry_migration" not in recovery["recovery_options"]:
        await candidate.close()
        pytest.fail("db_busy did not offer retry_migration")

    try:
        version = await candidate.retry_migration()
        assert candidate.database.status.mode is DatabaseAccessMode.read_write
        assert candidate.database.status.reason_code is None
        assert candidate.database.status.schema_version == version
    finally:
        await candidate.close()

    rebuilt = await create_memory_runtime(str(path), clock=clock)
    if isinstance(rebuilt, SafeModeMemoryRuntime):
        second_recovery = await rebuilt.recovery_status()
        await rebuilt.close()
        pytest.fail(
            "restart remained in safe mode after one db_busy retry: "
            f"{second_recovery['reason_code']}"
        )
    assert rebuilt._clock is clock
    return rebuilt


def _save_synthetic_memory(
    runtime: MemoryRuntime,
    *,
    created_at: datetime = _NOW,
) -> str:
    source = "synthetic-source-only::我喜欢合成咖啡"
    result = runtime.memory.consider_user_claim(
        MemoryClaim(
            memory_type=MemoryType.fact,
            canonical_key="preference:synthetic-coffee",
            content="用户喜欢合成咖啡",
            evidence_quote="我喜欢合成咖啡",
            importance_score=0.9,
            confidence_score=0.95,
            sensitivity_hint=MemorySensitivity.normal,
        ),
        user_id="synthetic-user",
        source_message_id="synthetic-message",
        source_input_mode=SourceInputMode.text,
        source_text=source,
        created_at=created_at,
    )
    assert result.item is not None
    return result.item.memory_id


def test_disabled_history_stops_runtime_access_but_retention_cleanup_continues(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock(_NOW)
        runtime = await _runtime(tmp_path / "history.sqlite3", clock)
        try:
            record = ConversationRecord(
                message_id="synthetic-history",
                session_id="synthetic-session",
                user_id="synthetic-user",
                turn_id="synthetic-turn",
                role=ConversationRole.user,
                origin=ConversationOrigin.user_text,
                content="synthetic history body",
                created_at=clock.now(),
            )
            assert runtime.history.record(record)
            disabled = await runtime.set_feature(FeatureName.recent_history, False)
            assert disabled.actual_state is FeatureActualState.disabled
            assert not runtime.history.record(record.model_copy(update={"message_id": "blocked"}))
            assert (
                runtime.history.recent(user_id="synthetic-user", session_id="synthetic-session")
                == []
            )

            clock.advance(timedelta(days=8))
            await runtime.run_maintenance_cycle()
            with runtime.database.connect() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
                    == 0
                )
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_logical_delete_stays_successful_while_physical_cleanup_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = FakeClock(_NOW)
        runtime = await _runtime(tmp_path / "delete.sqlite3", clock)
        await runtime.set_feature(FeatureName.long_term_memory, True)
        memory_id = _save_synthetic_memory(runtime)
        original_cleanup = runtime.database.secure_cleanup

        def locked_cleanup(*, vacuum: bool = False) -> None:
            del vacuum
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(runtime.database, "secure_cleanup", locked_cleanup)
        try:
            result = await runtime.delete_memory(memory_id, user_id="synthetic-user")
            assert result.logical_deleted
            assert result.cleanup_pending
            assert await runtime.list_memories(user_id="synthetic-user") == []
            assert "synthetic-source-only" not in json.dumps(
                await runtime.export(user_id="synthetic-user"), ensure_ascii=False
            )
            with runtime.database.connect() as connection:
                assert (
                    connection.execute(
                        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?",
                        ('"synthetic-source-only"',),
                    ).fetchall()
                    == []
                )

            failed = await runtime.run_maintenance_cycle()
            assert failed.error_codes == ("file_locked",)
            pending = await runtime.cleanup_status(result.cleanup_id or "")
            assert pending is not None
            assert pending.state.value == "retrying"
            assert pending.reason_code == "file_locked"

            clock.advance(timedelta(seconds=60))
            monkeypatch.setattr(runtime.database, "secure_cleanup", original_cleanup)
            recovered = await runtime.run_maintenance_cycle()
            assert recovered.error_codes == ()
            completed = await runtime.cleanup_status(result.cleanup_id or "")
            assert completed is not None and completed.state.value == "completed"
            assert (await runtime.check_health()).status.value == "ready"
        finally:
            monkeypatch.setattr(runtime.database, "secure_cleanup", original_cleanup)
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "frozen",
    [
        datetime(1999, 1, 2, 3, 4, 5, 678901, tzinfo=UTC),
        datetime(2099, 12, 30, 20, 19, 18, 123456, tzinfo=UTC),
    ],
    ids=["past", "future"],
)
def test_all_runtime_cleanup_creation_paths_use_only_the_injected_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen: datetime,
) -> None:
    class WallClockForbidden(datetime):
        @classmethod
        def now(cls, tz: object = None) -> WallClockForbidden:
            del tz
            raise AssertionError("cleanup creation read the wall clock")

    async def scenario() -> None:
        clock = FakeClock(frozen)
        runtime = await _runtime(tmp_path / f"all-paths-{frozen.year}.sqlite3", clock)
        monkeypatch.setattr(repositories_module, "datetime", WallClockForbidden)
        try:
            await runtime.set_feature(FeatureName.long_term_memory, True)

            memory_id = _save_synthetic_memory(runtime, created_at=frozen)
            deleted = await runtime.delete_memory(memory_id, user_id="synthetic-user")
            assert deleted.logical_deleted

            _save_synthetic_memory(runtime, created_at=frozen)
            cleared_memories = await runtime.clear_memories(user_id="synthetic-user")
            assert cleared_memories.logical_deleted

            current = ConversationRecord(
                message_id="synthetic-history-clear",
                session_id="synthetic-session",
                user_id="synthetic-user",
                turn_id="synthetic-turn-clear",
                role=ConversationRole.user,
                origin=ConversationOrigin.user_text,
                content="synthetic history clear body",
                created_at=frozen,
            )
            assert runtime.history.record(current)
            cleared_history = await runtime.clear_history(
                user_id="synthetic-user",
                session_id="synthetic-session",
            )
            assert cleared_history.logical_deleted

            expired = current.model_copy(
                update={
                    "message_id": "synthetic-history-retention",
                    "turn_id": "synthetic-turn-retention",
                    "created_at": frozen - timedelta(days=8),
                }
            )
            assert runtime.history.record(expired)
            assert runtime.history.cleanup() == 1

            expected_timestamp = frozen.isoformat(timespec="microseconds").replace("+00:00", "Z")
            with runtime.database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT kind, state, created_at, updated_at, next_attempt_at, completed_at
                    FROM physical_cleanup_jobs
                    ORDER BY kind
                    """
                ).fetchall()
            assert [str(row["kind"]) for row in rows] == [
                "history_clear",
                "history_retention",
                "memory_clear",
                "memory_delete",
            ]
            assert all(str(row["state"]) == "pending" for row in rows)
            assert all(row["completed_at"] is None for row in rows)
            assert all(
                (
                    str(row["created_at"]),
                    str(row["updated_at"]),
                    str(row["next_attempt_at"]),
                )
                == (expected_timestamp, expected_timestamp, expected_timestamp)
                for row in rows
            )
        finally:
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "frozen",
    [
        datetime(1998, 6, 7, 8, 9, 10, tzinfo=UTC),
        datetime(2101, 6, 7, 8, 9, 10, tzinfo=UTC),
    ],
    ids=["past", "future"],
)
def test_cleanup_retry_due_order_and_completion_survive_restart_with_frozen_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen: datetime,
) -> None:
    async def scenario() -> None:
        path = tmp_path / f"restart-{frozen.year}.sqlite3"
        clock = FakeClock(frozen)
        runtime = await _runtime(path, clock)
        await runtime.set_feature(FeatureName.long_term_memory, True)
        memory_id = _save_synthetic_memory(runtime, created_at=frozen)
        original_cleanup = runtime.database.secure_cleanup

        def locked_cleanup(*, vacuum: bool = False) -> None:
            del vacuum
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(runtime.database, "secure_cleanup", locked_cleanup)
        result = await runtime.delete_memory(memory_id, user_id="synthetic-user")
        assert result.cleanup_id is not None
        pending = await runtime.cleanup_status(result.cleanup_id)
        assert pending is not None
        assert pending.created_at == frozen
        assert pending.updated_at == frozen
        assert pending.next_attempt_at == frozen

        failed = await runtime.run_maintenance_cycle()
        assert failed.error_codes == ("file_locked",)
        retrying = await runtime.cleanup_status(result.cleanup_id)
        assert retrying is not None
        assert retrying.state.value == "retrying"
        assert retrying.attempt_count == 1
        assert retrying.updated_at == frozen
        assert retrying.next_attempt_at == frozen + timedelta(seconds=60)

        monkeypatch.setattr(runtime.database, "secure_cleanup", original_cleanup)
        await runtime.close()

        restarted = await _restart_runtime_after_single_busy_retry(path, clock)
        restart_cleanup = restarted.database.secure_cleanup
        cleanup_calls: list[bool] = []

        def recording_cleanup(*, vacuum: bool = False) -> None:
            cleanup_calls.append(vacuum)
            restart_cleanup(vacuum=vacuum)

        monkeypatch.setattr(restarted.database, "secure_cleanup", recording_cleanup)
        try:
            second_memory_id = _save_synthetic_memory(restarted, created_at=frozen)
            second = await restarted.delete_memory(
                second_memory_id,
                user_id="synthetic-user",
            )
            assert second.cleanup_id is not None

            immediate = await restarted.run_maintenance_cycle()
            assert immediate.error_codes == ()
            assert cleanup_calls == [False]
            second_completed = await restarted.cleanup_status(second.cleanup_id)
            first_waiting = await restarted.cleanup_status(result.cleanup_id)
            assert second_completed is not None
            assert second_completed.state.value == "completed"
            assert second_completed.completed_at == frozen
            assert first_waiting is not None
            assert first_waiting.state.value == "retrying"

            clock.advance(timedelta(seconds=59))
            await restarted.run_maintenance_cycle()
            assert cleanup_calls == [False]
            assert (await restarted.cleanup_status(result.cleanup_id)) == first_waiting

            clock.advance(timedelta(seconds=1))
            recovered = await restarted.run_maintenance_cycle()
            assert recovered.error_codes == ()
            assert cleanup_calls == [False, False]
            first_completed = await restarted.cleanup_status(result.cleanup_id)
            assert first_completed is not None
            assert first_completed.state.value == "completed"
            assert first_completed.completed_at == frozen + timedelta(seconds=60)
        finally:
            monkeypatch.setattr(restarted.database, "secure_cleanup", restart_cleanup)
            await restarted.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (sqlite3.OperationalError("database is locked"), "file_locked"),
        (sqlite3.OperationalError("database or disk is full"), "db_full"),
        (sqlite3.DatabaseError("database disk image is malformed"), "db_corrupt"),
    ],
)
def test_maintenance_isolates_faults_bounds_backoff_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    async def scenario() -> None:
        runtime = await _runtime(tmp_path / f"{expected_code}.sqlite3", FakeClock(_NOW))
        history_calls = 0
        original_history = runtime.history.cleanup

        def history_cleanup() -> int:
            nonlocal history_calls
            history_calls += 1
            return original_history()

        monkeypatch.setattr(runtime.history, "cleanup", history_cleanup)
        monkeypatch.setattr(
            runtime.memory,
            "prune_expired_confirmations",
            lambda: (_ for _ in ()).throw(failure),
        )
        try:
            delays: list[float] = []
            for _ in range(6):
                snapshot = await runtime.run_maintenance_cycle()
                delays.append(snapshot.next_delay_seconds)
            assert history_calls == 6
            assert snapshot.error_codes == (expected_code,)
            assert delays == sorted(delays)
            assert delays[-1] <= 900.0

            monkeypatch.setattr(runtime.memory, "prune_expired_confirmations", lambda: 0)
            recovered = await runtime.run_maintenance_cycle()
            assert recovered.error_codes == ()
            assert recovered.next_delay_seconds == 60.0
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_feature_transition_failure_is_visible_and_same_desired_state_retries(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await _runtime(tmp_path / "feature.sqlite3", FakeClock(_NOW))
        should_fail = True
        observed: list[int] = []

        async def handler(state: FeatureState) -> None:
            nonlocal should_fail
            generation = state.generation
            observed.append(generation)
            if should_fail:
                should_fail = False
                raise RuntimeError("synthetic transition body that must not be exposed")

        runtime.add_feature_transition_handler(handler)
        try:
            failed = await runtime.set_feature(FeatureName.vision, True)
            assert failed.actual_state is FeatureActualState.failed
            assert failed.reason_code == "feature_transition_failed"
            assert not failed.enabled

            recovered = await runtime.set_feature(FeatureName.vision, True)
            assert recovered.actual_state is FeatureActualState.enabled
            assert recovered.reason_code is None
            assert recovered.generation == failed.generation + 1
            assert observed == [failed.generation, recovered.generation]
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_safe_mode_creation_closes_owned_analyzer_for_future_and_migration_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        future_path = tmp_path / "synthetic-future.sqlite3"
        database = SQLiteDatabase(future_path)
        database.initialize()
        with database.connect() as connection, transaction(connection):
            connection.execute(
                "INSERT INTO schema_migrations VALUES (99, 'synthetic_future', 'now')"
            )
        database.secure_cleanup()

        future_analyzer = _CloseRecordingAnalyzer()
        future = await create_memory_runtime(str(future_path), analyzer=future_analyzer)
        assert isinstance(future, SafeModeMemoryRuntime)
        assert future_analyzer.close_calls == 1
        assert (await future.check_health()).error_code == "db_future_schema"
        await future.close()

        def migration_failure(_database: SQLiteDatabase) -> int:
            raise MigrationError("synthetic migration failure")

        monkeypatch.setattr(SQLiteDatabase, "initialize", migration_failure)
        failed_analyzer = _CloseRecordingAnalyzer()
        failed = await create_memory_runtime(
            str(tmp_path / "synthetic-migration-failure.sqlite3"),
            analyzer=failed_analyzer,
        )
        assert isinstance(failed, SafeModeMemoryRuntime)
        assert failed_analyzer.close_calls == 1
        recovery = await failed.recovery_status()
        assert recovery["reason_code"] == "db_migration_failed"
        assert recovery["backups"] == []

    asyncio.run(scenario())


def test_health_classifies_safe_mode_maintenance_and_repository_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = await _runtime(tmp_path / "health.sqlite3", FakeClock(_NOW))
        try:
            runtime.database.enter_safe_mode("db_corrupt")
            safe = await runtime.check_health()
            assert safe.status.value == "unavailable"
            assert safe.error_code == "db_corrupt"
            runtime.database._status = DatabaseStatus(
                DatabaseAccessMode.read_write,
                3,
                None,
            )

            runtime._maintenance_errors["confirmations"] = "db_full"
            maintenance = await runtime.check_health()
            assert maintenance.status.value == "degraded"
            assert maintenance.error_code == "db_full"
            runtime._maintenance_errors.clear()

            monkeypatch.setattr(
                runtime.cleanup_repository,
                "has_pending",
                lambda: (_ for _ in ()).throw(
                    sqlite3.DatabaseError("database disk image is malformed")
                ),
            )
            corrupt = await runtime.check_health()
            assert corrupt.status.value == "unavailable"
            assert corrupt.error_code == "db_corrupt"

            monkeypatch.setattr(
                runtime.cleanup_repository,
                "has_pending",
                lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk is full")),
            )
            full = await runtime.check_health()
            assert full.status.value == "degraded"
            assert full.error_code == "db_full"
        finally:
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("sqlite_errorcode", "expected"),
    [
        (sqlite3.SQLITE_BUSY, "db_busy"),
        (sqlite3.SQLITE_FULL, "db_full"),
        (sqlite3.SQLITE_CORRUPT, "db_corrupt"),
        (None, "maintenance_failed"),
    ],
)
def test_maintenance_error_codes_use_sqlite_codes_before_safe_text(
    sqlite_errorcode: int | None,
    expected: str,
) -> None:
    error = RuntimeError("synthetic opaque failure")
    if sqlite_errorcode is not None:
        error.sqlite_errorcode = sqlite_errorcode  # type: ignore[attr-defined]
    assert _maintenance_error_code(error) == expected


def test_close_aggregates_candidate_analyzer_and_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = await _runtime(tmp_path / "close-failure.sqlite3", FakeClock(_NOW))

        async def fail_close() -> None:
            raise RuntimeError("synthetic close failure")

        def fail_cleanup(*, vacuum: bool = False) -> None:
            del vacuum
            raise sqlite3.OperationalError("synthetic cleanup failure")

        monkeypatch.setattr(runtime.candidates, "close", fail_close)
        monkeypatch.setattr(runtime.analyzer, "close", fail_close)
        monkeypatch.setattr(runtime.database, "secure_cleanup", fail_cleanup)
        with pytest.raises(ExceptionGroup) as raised:
            await runtime.close()
        assert len(raised.value.exceptions) == 3

    asyncio.run(scenario())
