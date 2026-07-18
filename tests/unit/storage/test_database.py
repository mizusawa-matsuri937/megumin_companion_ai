from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.storage.database import (
    MigrationError,
    SQLiteDatabase,
    StorageError,
    apply_migrations,
    transaction,
)
from app.storage.migrations.v001_initial import MIGRATIONS as INITIAL_MIGRATIONS
from app.storage.migrations.v001_initial import Migration


def test_initial_migration_is_complete_and_idempotent(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "companion.sqlite3")

    assert database.initialize() == 2
    assert database.initialize() == 2

    with database.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "conversation_messages",
            "memories",
            "memory_sources",
            "user_profiles",
            "feature_flags",
            "memories_fts",
            "schema_migrations",
            "idempotency_sessions",
            "idempotency_turns",
        } <= tables
        versions = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
        assert [tuple(row) for row in versions] == [
            (1, "initial_history_memory_features_fts"),
            (2, "message_idempotency"),
        ]


def test_v2_upgrade_checkpoints_and_keeps_a_verified_v1_backup(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "companion.sqlite3")
    backup_path = tmp_path / "companion.sqlite3.pre-v1-to-v2.backup"
    with database.connect() as connection:
        assert apply_migrations(connection, migrations=INITIAL_MIGRATIONS) == 1
        stale_backup = sqlite3.connect(backup_path, isolation_level=None)
        try:
            connection.backup(stale_backup)
        finally:
            stale_backup.close()
        connection.execute(
            """
            INSERT INTO conversation_messages(
                message_id, session_id, user_id, turn_id, role, origin, content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "message-before-v2",
                "session-before-v2",
                "user-before-v2",
                "turn-before-v2",
                "user",
                "user_text",
                "migration-backup-sentinel",
                "2026-07-18T12:00:00.000000Z",
            ),
        )

    assert database.initialize() == 2
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert (
            backup.execute(
                "SELECT content FROM conversation_messages WHERE message_id = ?",
                ("message-before-v2",),
            ).fetchone()[0]
            == "migration-backup-sentinel"
        )
        assert (
            backup.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("idempotency_turns",),
            ).fetchone()
            is None
        )


def test_invalid_existing_migration_backup_fails_closed_before_schema_write(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "companion.sqlite3")
    with database.connect() as connection:
        assert apply_migrations(connection, migrations=INITIAL_MIGRATIONS) == 1
    (tmp_path / "companion.sqlite3.pre-v1-to-v2.backup").write_bytes(b"not-sqlite")

    with pytest.raises(MigrationError, match="backup"):
        database.initialize()

    with database.connect() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("idempotency_turns",),
            ).fetchone()
            is None
        )


def test_connection_enables_integrity_and_privacy_pragmas(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "companion.sqlite3", busy_timeout_ms=1234)
    database.initialize()

    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert (
            connection.execute(
                "SELECT v FROM memories_fts_config WHERE k = 'secure-delete'"
            ).fetchone()[0]
            == 1
        )


def test_failed_migration_rolls_back_every_statement() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    failing = Migration(
        version=1,
        name="failing",
        applied_at="2026-07-13T00:00:00Z",
        statements=(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)",
            "CREATE TABLE should_rollback(value TEXT)",
            "THIS IS NOT SQL",
        ),
    )

    with pytest.raises(MigrationError, match="migration 1"):
        apply_migrations(connection, migrations=(failing,))

    assert (
        connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []
    )


def test_unversioned_nonempty_database_is_refused() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("CREATE TABLE unexpected(value TEXT)")

    with pytest.raises(MigrationError, match="unversioned"):
        apply_migrations(connection)
    assert (
        connection.execute("SELECT name FROM sqlite_master WHERE name = 'unexpected'").fetchone()
        is not None
    )


def test_future_schema_version_is_refused(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "future.sqlite3")
    database.initialize()
    with database.connect() as connection, transaction(connection):
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (99, 'future', 'now')"
        )
    with database.connect() as connection, pytest.raises(MigrationError, match="newer"):
        apply_migrations(connection)


def test_explicit_transaction_rolls_back_and_rejects_nesting() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("CREATE TABLE values_table(value TEXT)")
    with pytest.raises(RuntimeError, match="boom"), transaction(connection):
        connection.execute("INSERT INTO values_table VALUES ('secret')")
        raise RuntimeError("boom")
    assert connection.execute("SELECT * FROM values_table").fetchall() == []

    with (
        transaction(connection),
        pytest.raises(StorageError, match="nested"),
        transaction(connection),
    ):
        pass


def test_migration_sequence_must_be_contiguous() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    invalid = Migration(2, "gap", "now", ())
    with pytest.raises(MigrationError, match="contiguous"):
        apply_migrations(connection, migrations=(invalid,))


def test_concurrent_initialization_serializes_schema_ownership(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "concurrent.sqlite3")
    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = list(executor.map(lambda _index: database.initialize(), range(2)))

    assert versions == [2, 2]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2


def test_database_and_transaction_reject_invalid_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="busy_timeout"):
        SQLiteDatabase(tmp_path / "invalid.sqlite3", busy_timeout_ms=-1)

    connection = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(ValueError, match="unsupported"), transaction(connection, mode="invalid"):
        pass
    with transaction(connection), pytest.raises(MigrationError, match="idle"):
        apply_migrations(connection)
