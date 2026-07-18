"""Small SQLite boundary with explicit transactions and privacy-oriented pragmas."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.storage.migrations.v001_initial import MIGRATIONS as INITIAL_MIGRATIONS
from app.storage.migrations.v001_initial import Migration
from app.storage.migrations.v002_idempotency import MIGRATION as IDEMPOTENCY_MIGRATION

MIGRATIONS = (*INITIAL_MIGRATIONS, IDEMPOTENCY_MIGRATION)
_INITIALIZE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class MigrationBackup:
    name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class MigrationAudit:
    migration_id: str
    from_version: int
    to_version: int
    started_at: str
    backup: MigrationBackup | None = None


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
        # The process-local lock covers the pre-migration backup and the schema write as
        # one startup operation. Cross-process single-instance ownership is introduced
        # later in W15; SQLite still serializes the schema transaction itself.
        with _INITIALIZE_LOCK, self.connect() as connection:
            current = _current_version(connection)
            latest = MIGRATIONS[-1].version if MIGRATIONS else 0
            audit = MigrationAudit(
                migration_id=f"migration_{uuid4().hex}",
                from_version=current,
                to_version=latest,
                started_at=_utc_timestamp(),
            )
            if 0 < current < latest:
                audit = replace(
                    audit,
                    backup=self._ensure_migration_backup(
                        connection,
                        from_version=current,
                        to_version=latest,
                    ),
                )
            return apply_migrations(connection, audit=audit)

    def _ensure_migration_backup(
        self,
        connection: sqlite3.Connection,
        *,
        from_version: int,
        to_version: int,
    ) -> MigrationBackup:
        if connection.in_transaction:
            raise MigrationError("migration backup requires an idle connection")
        backup_path = self.path.with_name(
            f"{self.path.name}.pre-v{from_version}-to-v{to_version}.backup"
        )
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if backup_path.exists():
                _verify_migration_backup(backup_path, expected_version=from_version)

            temporary = backup_path.with_name(f".{backup_path.name}.{uuid4().hex}.tmp")
            destination: sqlite3.Connection | None = None
            try:
                destination = sqlite3.connect(temporary, isolation_level=None)
                connection.backup(destination)
                destination.close()
                destination = None
                _verify_migration_backup(temporary, expected_version=from_version)
                temporary.replace(backup_path)
                _verify_migration_backup(backup_path, expected_version=from_version)
            finally:
                if destination is not None:
                    destination.close()
                temporary.unlink(missing_ok=True)
            return MigrationBackup(
                name=backup_path.name,
                sha256=_sha256_file(backup_path),
            )
        except (OSError, sqlite3.Error) as exc:
            raise MigrationError(
                f"migration backup v{from_version} to v{to_version} failed"
            ) from exc

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
    audit: MigrationAudit | None = None,
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
            from_version = current
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
            if current > from_version and _table_exists(connection, "migration_audit"):
                record = audit or MigrationAudit(
                    migration_id=f"migration_{uuid4().hex}",
                    from_version=from_version,
                    to_version=current,
                    started_at=_utc_timestamp(),
                )
                if record.from_version != from_version or record.to_version != current:
                    raise MigrationError(
                        "migration audit version range does not match schema write"
                    )
                connection.execute(
                    """
                    INSERT INTO migration_audit(
                        migration_id, from_version, to_version, started_at, completed_at,
                        backup_name, backup_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.migration_id,
                        record.from_version,
                        record.to_version,
                        record.started_at,
                        _utc_timestamp(),
                        record.backup.name if record.backup is not None else None,
                        record.backup.sha256 if record.backup is not None else None,
                    ),
                )
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


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


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


def _verify_migration_backup(path: Path, *, expected_version: int) -> None:
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise MigrationError("migration backup integrity check failed")
            version = _current_version(connection)
            if version != expected_version:
                raise MigrationError(
                    "migration backup schema version does not match the upgrade source"
                )
    except sqlite3.Error as exc:
        raise MigrationError("migration backup verification failed") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
