"""Small SQLite boundary with explicit transactions and privacy-oriented pragmas."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.storage.migrations.v001_initial import MIGRATIONS, Migration


class StorageError(RuntimeError):
    """Base class for local persistence failures."""


class MigrationError(StorageError):
    """Raised when a database cannot be safely migrated."""


class StorageConflictError(StorageError):
    """Raised when an idempotency key is reused with different content."""


class SQLiteDatabase:
    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA secure_delete = ON")
            _enable_wal(connection, busy_timeout_ms=self.busy_timeout_ms)
            connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> int:
        with self.connect() as connection:
            return apply_migrations(connection)

    def secure_cleanup(self, *, vacuum: bool = False) -> None:
        """Move committed deletes out of the WAL and optionally compact free pages."""

        with self.connect() as connection:
            if connection.in_transaction:
                raise StorageError("secure cleanup cannot run inside a transaction")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if vacuum:
                connection.execute("VACUUM")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@contextmanager
def transaction(connection: sqlite3.Connection, *, mode: str = "IMMEDIATE") -> Iterator[None]:
    """Run one explicit transaction; nested/implicit transactions are forbidden."""

    normalized = mode.upper()
    if normalized not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
        raise ValueError("unsupported transaction mode")
    if connection.in_transaction:
        raise StorageError("nested transactions are not supported")
    connection.execute(f"BEGIN {normalized}")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> int:
    """Apply ordered migrations statement-by-statement in rollback-safe transactions."""

    if connection.in_transaction:
        raise MigrationError("migrations require an idle connection")
    versions = tuple(migration.version for migration in migrations)
    if versions != tuple(range(1, len(migrations) + 1)):
        raise MigrationError("migration versions must be contiguous and start at one")
    latest = versions[-1] if versions else 0
    active: Migration | None = None
    try:
        # The write lock is acquired before reading schema state. Two concurrent startup
        # attempts therefore cannot both conclude that they own a version-zero database.
        with transaction(connection):
            current = _current_version(connection)
            if current > latest:
                raise MigrationError(
                    f"database schema version {current} is newer than supported version {latest}"
                )
            if current == 0:
                unexpected = _user_tables(connection)
                if unexpected:
                    raise MigrationError(
                        "unversioned database contains tables and will not be modified: "
                        + ", ".join(sorted(unexpected))
                    )
            for migration in migrations:
                if migration.version <= current:
                    continue
                active = migration
                for statement in migration.statements:
                    # Do not replace this with executescript(): sqlite3 executescript() performs
                    # an implicit commit and would make a failed migration non-atomic.
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, migration.applied_at),
                )
                current = migration.version
    except sqlite3.Error as exc:
        label = (
            f"migration {active.version} ({active.name})"
            if active is not None
            else "migration transaction"
        )
        raise MigrationError(f"{label} failed") from exc
    return current


def _current_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    assert row is not None
    return int(row[0])


def _user_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _enable_wal(connection: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
    # SQLite's journal-mode pragma can report SQLITE_BUSY before its configured busy
    # handler is invoked. Retry only that well-defined startup race within the same bound.
    deadline = time.monotonic() + busy_timeout_ms / 1_000
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).casefold() or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
