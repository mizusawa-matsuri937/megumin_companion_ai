from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import app.storage.database as database_module
import pytest
from app.memory.runtime import MemoryRuntime, create_memory_runtime
from app.schemas import FeatureActualState, FeatureName
from app.storage import (
    DatabaseSafeModeError,
    FeatureFlagRepository,
    MigrationError,
    SQLiteDatabase,
    StorageConflictError,
    StorageReadOnlyError,
)
from app.storage.database import MIGRATIONS, apply_migrations, transaction
from hypothesis import given, settings
from hypothesis import strategies as st

_OLD_SOURCE = "old-source-sentinel::full dialogue that must not survive active v3 surfaces"
_EVIDENCE = "用户喜欢合成测试咖啡"


def _create_v2_database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    with database.connect() as connection:
        assert apply_migrations(connection, migrations=MIGRATIONS[:2]) == 2
        connection.execute(
            """
            INSERT INTO memories(
                memory_id, user_id, memory_type, canonical_key, content,
                normalized_content, importance_score, confidence_score, sensitivity,
                status, source_kind, source_message_id, source_excerpt, related_emotion,
                created_at, updated_at, last_seen_at
            ) VALUES (
                'mem-legacy', 'synthetic-user', 'fact', 'preference:synthetic-coffee', ?,
                '用户喜欢合成测试咖啡', 0.9, 0.95, 'normal', 'active', 'dialogue',
                'synthetic-message', ?, NULL, ?, ?, ?
            )
            """,
            (
                _EVIDENCE,
                f"{_OLD_SOURCE}; {_EVIDENCE}",
                "2026-07-18T00:00:00.000000Z",
                "2026-07-18T00:00:00.000000Z",
                "2026-07-18T00:00:00.000000Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO memory_sources(
                source_id, memory_id, source_kind, source_message_id, source_excerpt, observed_at
            ) VALUES ('source-legacy', 'mem-legacy', 'dialogue', 'synthetic-message', ?, ?)
            """,
            (
                f"{_OLD_SOURCE}; {_EVIDENCE}",
                "2026-07-18T00:00:00.000000Z",
            ),
        )
    return database


def test_v2_to_v3_redacts_old_source_from_live_query_fts_export_and_manifest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic.sqlite3"
    database = _create_v2_database(path)

    assert database.initialize() == 3
    backup = tmp_path / "synthetic.sqlite3.pre-v2-to-v3.backup"
    assert backup.is_file()
    assert _OLD_SOURCE.encode() in backup.read_bytes()

    with database.connect() as connection:
        memory = connection.execute(
            "SELECT source_excerpt, source_sha256, provenance FROM memories"
        ).fetchone()
        source = connection.execute(
            "SELECT source_excerpt, source_sha256, provenance FROM memory_sources"
        ).fetchone()
        assert memory is not None and source is not None
        expected_hash = hashlib.sha256(f"{_OLD_SOURCE}; {_EVIDENCE}".encode()).hexdigest()
        assert tuple(memory) == (_EVIDENCE, expected_hash, "legacy_unverified")
        assert tuple(source) == (_EVIDENCE, expected_hash, "legacy_unverified")
        assert (
            connection.execute(
                "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?",
                ('"old-source-sentinel"',),
            ).fetchall()
            == []
        )
        dump = "\n".join(connection.iterdump())
        audit = [dict(row) for row in connection.execute("SELECT * FROM migration_audit")]
    assert _OLD_SOURCE not in dump
    assert _OLD_SOURCE not in json.dumps(audit, sort_keys=True)

    async def export() -> dict[str, object]:
        runtime = await create_memory_runtime(str(path))
        assert isinstance(runtime, MemoryRuntime)
        try:
            return await runtime.export(user_id="synthetic-user")
        finally:
            await runtime.close()

    exported = json.dumps(asyncio.run(export()), ensure_ascii=False, sort_keys=True)
    assert _OLD_SOURCE not in exported
    assert _EVIDENCE in exported

    database.secure_cleanup(vacuum=True)
    assert _OLD_SOURCE.encode() not in path.read_bytes()
    manifest = {
        "name": backup.name,
        "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    }
    assert _OLD_SOURCE not in json.dumps(manifest, sort_keys=True)
    artifact_hits = {
        artifact.name
        for artifact in tmp_path.iterdir()
        if artifact.is_file() and _OLD_SOURCE.encode() in artifact.read_bytes()
    }
    assert artifact_hits == {backup.name}
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_checkpoint",
        "after_backup",
        "before_migration",
        "before_v3_statement_0",
        "after_v3_statement_8",
        "before_migration_commit",
    ],
)
def test_v3_fault_injection_preserves_original_v2_data(
    tmp_path: Path,
    fault_stage: str,
) -> None:
    path = tmp_path / f"fault-{fault_stage}.sqlite3"
    _create_v2_database(path)

    def inject(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError("synthetic migration interruption")

    database = SQLiteDatabase(path, migration_fault_injector=inject)
    with pytest.raises(RuntimeError, match="synthetic migration interruption"):
        database.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT source_excerpt FROM memories WHERE memory_id = 'mem-legacy'"
            ).fetchone()[0]
            == f"{_OLD_SOURCE}; {_EVIDENCE}"
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'physical_cleanup_jobs'"
            ).fetchone()
            is None
        )


def test_wal_writer_lock_blocks_checkpoint_before_migration_and_then_recovers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wal-lock.sqlite3"
    _create_v2_database(path)
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE memories SET updated_at = '2026-07-18T00:00:01.000000Z' "
            "WHERE memory_id = 'mem-legacy'"
        )
        locked = SQLiteDatabase(path, busy_timeout_ms=0)
        with pytest.raises(MigrationError, match="busy|locked"):
            locked.initialize()
    finally:
        writer.rollback()
        writer.close()

    assert SQLiteDatabase(path).initialize() == 3


def test_future_schema_enters_zero_write_safe_mode_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "future.sqlite3"
    database = SQLiteDatabase(path)
    database.initialize()
    with database.connect() as connection, transaction(connection):
        connection.execute("INSERT INTO schema_migrations VALUES (99, 'synthetic_future', 'now')")
    before = {
        candidate.name: (candidate.read_bytes(), candidate.stat().st_mtime_ns)
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    }

    future = SQLiteDatabase(path)
    with pytest.raises(DatabaseSafeModeError) as raised:
        future.initialize()

    assert raised.value.status.reason_code == "db_future_schema"
    assert future.status.mode.value == "safe_read_only"
    with future.connect() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 99
        with pytest.raises(StorageReadOnlyError), transaction(connection):
            pass
    after = {
        candidate.name: (candidate.read_bytes(), candidate.stat().st_mtime_ns)
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    }
    assert after == before


def test_future_schema_committed_in_live_wal_is_not_hidden_by_read_only_probe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "future-in-wal.sqlite3"
    SQLiteDatabase(path).initialize()
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO schema_migrations VALUES (99, 'future_in_wal', 'now')")
        writer.commit()
        wal = Path(f"{path}-wal")
        assert wal.is_file() and wal.stat().st_size > 0

        future = SQLiteDatabase(path)
        with pytest.raises(DatabaseSafeModeError) as raised:
            future.initialize()
        assert raised.value.status.schema_version == 99
        assert raised.value.status.reason_code == "db_future_schema"
        assert not list(tmp_path.glob(".future-in-wal.sqlite3.probe-*"))
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("failure", "reason_code", "options"),
    [
        (
            database_module._ProbeSnapshotChanged(),
            "db_busy",
            ["retry_migration", "keep_read_only"],
        ),
        (
            PermissionError("synthetic snapshot permission failure"),
            "db_busy",
            [
                "retry_migration",
                "keep_read_only",
            ],
        ),
        (
            sqlite3.DatabaseError("synthetic invalid database"),
            "db_corrupt",
            [
                "restore_backup",
            ],
        ),
    ],
)
def test_probe_faults_fail_closed_without_database_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    reason_code: str,
    options: list[str],
) -> None:
    path = tmp_path / f"probe-{reason_code}-{type(failure).__name__}.sqlite3"
    database = SQLiteDatabase(path)
    database.initialize()
    before = path.read_bytes()

    class FailingSnapshot:
        def __enter__(self) -> tuple[Path, bool]:
            raise failure

        def __exit__(self, *_args: object) -> None:
            return None

    def fail_snapshot(_path: Path) -> FailingSnapshot:
        return FailingSnapshot()

    monkeypatch.setattr(database_module, "_read_only_snapshot", fail_snapshot)
    status = SQLiteDatabase(path).probe()
    assert status.mode.value == "safe_read_only"
    assert status.reason_code == reason_code
    assert [item.value for item in status.recovery_options] == options
    assert path.read_bytes() == before


def test_explicit_verified_backup_restore_preserves_quarantine_and_reopens_v2(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restore.sqlite3"
    database = _create_v2_database(path)
    assert database.initialize() == 3
    backup = tmp_path / "restore.sqlite3.pre-v2-to-v3.backup"
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()

    database.enter_safe_mode("db_migration_failed")
    with pytest.raises(MigrationError, match="name"):
        database.restore_verified_backup("../outside.backup", digest)
    with pytest.raises(MigrationError, match="hash"):
        database.restore_verified_backup(backup.name, "0" * 64)
    invalid = tmp_path / "restore.sqlite3.pre-v-invalid-to-v3.backup"
    invalid.write_bytes(b"not sqlite")
    listed = database.list_verified_backups()
    assert [(item.name, item.sha256) for item in listed] == [(backup.name, digest)]
    Path(f"{path}-wal").write_bytes(b"synthetic wal quarantine sentinel")
    Path(f"{path}-shm").write_bytes(b"synthetic shm quarantine sentinel")
    assert database.restore_verified_backup(backup.name, digest) == 2

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT source_excerpt FROM memories WHERE memory_id = 'mem-legacy'"
            ).fetchone()[0]
            == f"{_OLD_SOURCE}; {_EVIDENCE}"
        )
    assert list(tmp_path.glob("restore.sqlite3.recovery-*.quarantine"))
    assert list(tmp_path.glob("restore.sqlite3.recovery-*.quarantine-wal"))
    assert list(tmp_path.glob("restore.sqlite3.recovery-*.quarantine-shm"))


@settings(max_examples=30, deadline=None)
@given(st.lists(st.booleans(), min_size=1, max_size=40))
def test_feature_state_machine_preserves_generation_and_stable_invariants(
    desired_sequence: list[bool],
) -> None:
    with TemporaryDirectory(prefix="w10-feature-state-") as directory:
        database = SQLiteDatabase(Path(directory) / "synthetic.sqlite3")
        database.initialize()
        repository = FeatureFlagRepository(database)
        now = datetime(2026, 7, 18, 12, tzinfo=UTC)
        previous = repository.get(FeatureName.long_term_memory)

        for desired in desired_sequence:
            transition = repository.request_transition(
                FeatureName.long_term_memory,
                desired,
                updated_at=now,
            )
            if transition.actual_state in {
                FeatureActualState.enabling,
                FeatureActualState.disabling,
            }:
                assert transition.generation == previous.generation + 1
                with pytest.raises(StorageConflictError, match="stale"):
                    repository.finish_transition(
                        FeatureName.long_term_memory,
                        generation=transition.generation + 1,
                        actual_state=FeatureActualState.enabled,
                        reason_code=None,
                        updated_at=now,
                    )
                with pytest.raises(StorageConflictError, match="inconsistent"):
                    repository.finish_transition(
                        FeatureName.long_term_memory,
                        generation=transition.generation,
                        actual_state=(
                            FeatureActualState.disabled if desired else FeatureActualState.enabled
                        ),
                        reason_code=None,
                        updated_at=now,
                    )
                previous = repository.finish_transition(
                    FeatureName.long_term_memory,
                    generation=transition.generation,
                    actual_state=(
                        FeatureActualState.enabled if desired else FeatureActualState.disabled
                    ),
                    reason_code=None,
                    updated_at=now,
                )
            else:
                assert transition.generation == previous.generation
                previous = transition
            assert previous.enabled is desired
            now += timedelta(microseconds=1)


def test_interrupted_transition_reconciles_to_explicit_failed_state(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "interrupted.sqlite3")
    database.initialize()
    repository = FeatureFlagRepository(database)
    transition = repository.request_transition(
        FeatureName.vision,
        True,
        updated_at=datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    assert transition.actual_state is FeatureActualState.enabling

    states = repository.reconcile_interrupted(updated_at=datetime(2026, 7, 18, 12, 1, tzinfo=UTC))
    recovered = next(state for state in states if state.name is FeatureName.vision)
    assert recovered.desired_enabled
    assert recovered.actual_state is FeatureActualState.failed
    assert recovered.reason_code == "interrupted_transition"


def test_transient_migration_safe_mode_offers_retry_and_recovers(tmp_path: Path) -> None:
    path = tmp_path / "retry.sqlite3"
    _create_v2_database(path)

    def interrupt(stage: str) -> None:
        if stage == "before_migration":
            raise RuntimeError("synthetic transient interruption")

    database = SQLiteDatabase(path, migration_fault_injector=interrupt)
    with pytest.raises(RuntimeError, match="transient"):
        database.initialize()
    status = database.enter_safe_mode("db_migration_failed")
    assert [item.value for item in status.recovery_options] == [
        "retry_migration",
        "restore_backup",
        "keep_read_only",
    ]

    database._migration_fault_injector = None
    assert database.retry_initialize() == 3
    assert database.status.mode.value == "read_write"
