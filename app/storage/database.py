"""Small SQLite boundary with explicit transactions and privacy-oriented pragmas."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.storage.migrations.v001_initial import MIGRATIONS as INITIAL_MIGRATIONS
from app.storage.migrations.v001_initial import Migration
from app.storage.migrations.v002_idempotency import MIGRATION as IDEMPOTENCY_MIGRATION
from app.storage.migrations.v003_memory_state_recovery import (
    MIGRATION as MEMORY_STATE_RECOVERY_MIGRATION,
)

MIGRATIONS = (*INITIAL_MIGRATIONS, IDEMPOTENCY_MIGRATION, MEMORY_STATE_RECOVERY_MIGRATION)
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


class DatabaseAccessMode(StrEnum):
    read_write = "read_write"
    safe_read_only = "safe_read_only"


class RecoveryChoice(StrEnum):
    retry_migration = "retry_migration"
    restore_backup = "restore_backup"
    keep_read_only = "keep_read_only"


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    mode: DatabaseAccessMode
    schema_version: int | None
    reason_code: str | None
    recovery_options: tuple[RecoveryChoice, ...] = ()


class StorageError(RuntimeError):
    """Base class for local persistence failures."""


class MigrationError(StorageError):
    """Raised when a database cannot be safely migrated."""


class DatabaseSafeModeError(MigrationError):
    """Raised after startup has entered an explicit read-only recovery mode."""

    def __init__(self, status: DatabaseStatus) -> None:
        self.status = status
        super().__init__(status.reason_code or "database_safe_mode")


class StorageReadOnlyError(StorageError):
    """Raised before a repository can mutate a safe-mode database."""


class StorageConflictError(StorageError):
    """Raised when an idempotency key is reused with different content."""


class _ProbeSnapshotChanged(RuntimeError):
    pass


class SQLiteDatabase:
    def __init__(
        self,
        path: Path | str,
        *,
        busy_timeout_ms: int = 5_000,
        migration_fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._migration_fault_injector = migration_fault_injector
        self._status = DatabaseStatus(
            mode=DatabaseAccessMode.read_write,
            schema_version=None,
            reason_code=None,
        )

    @property
    def status(self) -> DatabaseStatus:
        return self._status

    def probe(self) -> DatabaseStatus:
        """Inspect schema/integrity without creating files or changing journal state."""

        if not self.path.exists():
            return DatabaseStatus(DatabaseAccessMode.read_write, 0, None)
        try:
            with _read_only_snapshot(self.path) as (target, immutable):
                return _probe_database_file(target, immutable=immutable)
        except _ProbeSnapshotChanged:
            return DatabaseStatus(
                DatabaseAccessMode.safe_read_only,
                None,
                "db_busy",
                (RecoveryChoice.retry_migration, RecoveryChoice.keep_read_only),
            )
        except OSError:
            return DatabaseStatus(
                DatabaseAccessMode.safe_read_only,
                None,
                "db_busy",
                (RecoveryChoice.retry_migration, RecoveryChoice.keep_read_only),
            )
        except sqlite3.Error:
            return DatabaseStatus(
                DatabaseAccessMode.safe_read_only,
                None,
                "db_corrupt",
                (RecoveryChoice.restore_backup,),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        safe_read_only = self._status.mode is DatabaseAccessMode.safe_read_only
        if safe_read_only:
            with _read_only_snapshot(self.path) as (target, immutable):
                query = "mode=ro&immutable=1" if immutable else "mode=ro"
                uri = f"{target.resolve().as_uri()}?{query}"
                connection = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=self.busy_timeout_ms / 1_000,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                try:
                    connection.execute("PRAGMA foreign_keys = ON")
                    connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
                    connection.execute("PRAGMA query_only = ON")
                    yield connection
                finally:
                    connection.close()
            return
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
        with _INITIALIZE_LOCK:
            status = self.probe()
            self._status = status
            if status.mode is DatabaseAccessMode.safe_read_only:
                raise DatabaseSafeModeError(status)
            with self.connect() as connection:
                return self._initialize_connection(connection)

    def _initialize_connection(self, connection: sqlite3.Connection) -> int:
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
        injector = self._migration_fault_injector
        if injector is not None:
            injector("before_migration")
        version = apply_migrations(connection, audit=audit, fault_injector=injector)
        self._status = DatabaseStatus(DatabaseAccessMode.read_write, version, None)
        return version

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
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise MigrationError("migration checkpoint is busy")
            if self._migration_fault_injector is not None:
                self._migration_fault_injector("after_checkpoint")
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
            backup = MigrationBackup(
                name=backup_path.name,
                sha256=_sha256_file(backup_path),
            )
            if self._migration_fault_injector is not None:
                self._migration_fault_injector("after_backup")
            return backup
        except (OSError, sqlite3.Error) as exc:
            raise MigrationError(
                f"migration backup v{from_version} to v{to_version} failed"
            ) from exc

    def secure_cleanup(self, *, vacuum: bool = False) -> None:
        """Move committed deletes out of the WAL and optionally compact free pages."""

        with self.connect() as connection:
            if connection.in_transaction:
                raise StorageError("secure cleanup cannot run inside a transaction")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise StorageError("database cleanup checkpoint is busy")
            if vacuum:
                connection.execute("VACUUM")
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and int(checkpoint[0]) != 0:
                    raise StorageError("database cleanup checkpoint is busy")

    def enter_safe_mode(self, reason_code: str) -> DatabaseStatus:
        current = self.probe().schema_version
        self._status = DatabaseStatus(
            DatabaseAccessMode.safe_read_only,
            current,
            reason_code,
            (
                RecoveryChoice.retry_migration,
                RecoveryChoice.restore_backup,
                RecoveryChoice.keep_read_only,
            ),
        )
        return self._status

    def retry_initialize(self) -> int:
        self._status = DatabaseStatus(DatabaseAccessMode.read_write, None, None)
        return self.initialize()

    def list_verified_backups(self) -> tuple[MigrationBackup, ...]:
        backups: list[MigrationBackup] = []
        for path in sorted(self.path.parent.glob(f"{self.path.name}.pre-v*-to-v*.backup")):
            try:
                _verified_backup_version(path)
                backups.append(MigrationBackup(name=path.name, sha256=_sha256_file(path)))
            except (OSError, MigrationError):
                continue
        return tuple(backups[:32])

    def restore_verified_backup(self, name: str, expected_sha256: str) -> int:
        """Apply an explicit offline restore choice through verified sibling staging."""

        if Path(name).name != name or not name.startswith(f"{self.path.name}.pre-v"):
            raise MigrationError("invalid recovery backup name")
        backup = self.path.with_name(name)
        if not backup.is_file() or _sha256_file(backup) != expected_sha256:
            raise MigrationError("recovery backup hash mismatch")
        version = _verified_backup_version(backup)
        temporary = self.path.with_name(f".{self.path.name}.recovery-{uuid4().hex}.tmp")
        quarantine = self.path.with_name(f"{self.path.name}.recovery-{uuid4().hex}.quarantine")
        moved_sidecars: list[tuple[Path, Path]] = []
        try:
            source = sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True)
            destination = sqlite3.connect(temporary, isolation_level=None)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            _verify_migration_backup(temporary, expected_version=version)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.path}{suffix}")
                if sidecar.exists():
                    sidecar_quarantine = Path(f"{quarantine}{suffix}")
                    os.replace(sidecar, sidecar_quarantine)
                    moved_sidecars.append((sidecar, sidecar_quarantine))
            if self.path.exists():
                os.replace(self.path, quarantine)
            try:
                os.replace(temporary, self.path)
            except BaseException:
                if quarantine.exists() and not self.path.exists():
                    os.replace(quarantine, self.path)
                for sidecar, sidecar_quarantine in moved_sidecars:
                    if sidecar_quarantine.exists() and not sidecar.exists():
                        os.replace(sidecar_quarantine, sidecar)
                raise
        except (OSError, sqlite3.Error) as exc:
            if quarantine.exists() and not self.path.exists():
                with suppress(OSError):
                    os.replace(quarantine, self.path)
            for sidecar, sidecar_quarantine in moved_sidecars:
                if sidecar_quarantine.exists() and not sidecar.exists():
                    with suppress(OSError):
                        os.replace(sidecar_quarantine, sidecar)
            raise MigrationError("verified backup restore failed") from exc
        finally:
            temporary.unlink(missing_ok=True)
        self._status = DatabaseStatus(DatabaseAccessMode.read_write, version, None)
        return version


def _probe_database_file(path: Path, *, immutable: bool) -> DatabaseStatus:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"{path.resolve().as_uri()}?{query}"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        current = _current_version(connection)
        latest = MIGRATIONS[-1].version if MIGRATIONS else 0
        if current > latest:
            return DatabaseStatus(
                DatabaseAccessMode.safe_read_only,
                current,
                "db_future_schema",
                (RecoveryChoice.keep_read_only,),
            )
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            return DatabaseStatus(
                DatabaseAccessMode.safe_read_only,
                current,
                "db_corrupt",
                (
                    RecoveryChoice.restore_backup,
                    RecoveryChoice.keep_read_only,
                ),
            )
        return DatabaseStatus(DatabaseAccessMode.read_write, current, None)


@contextmanager
def _read_only_snapshot(path: Path) -> Iterator[tuple[Path, bool]]:
    """Yield an original immutable file or a WAL-aware sibling copy, then erase it."""

    wal = Path(f"{path}-wal")
    if not wal.is_file() or wal.stat().st_size == 0:
        yield path, True
        return
    # A direct read-only SQLite connection may create or update WAL/SHM sidecars.
    # Copy the three-file snapshot so original future-schema bytes stay untouched.
    with TemporaryDirectory(prefix=f".{path.name}.probe-", dir=path.parent) as directory:
        snapshot = Path(directory) / path.name
        candidates = (path, wal, Path(f"{path}-shm"))
        before = {
            candidate: (candidate.stat().st_size, candidate.stat().st_mtime_ns)
            for candidate in candidates
            if candidate.exists()
        }
        for source in before:
            suffix = source.name.removeprefix(path.name)
            shutil.copyfile(source, Path(f"{snapshot}{suffix}"))
        after = {
            candidate: (candidate.stat().st_size, candidate.stat().st_mtime_ns)
            for candidate in before
            if candidate.exists()
        }
        if after != before or len(after) != len(before):
            raise _ProbeSnapshotChanged
        yield snapshot, False


@contextmanager
def transaction(connection: sqlite3.Connection, *, mode: str = "IMMEDIATE") -> Iterator[None]:
    """Run one explicit transaction; nested/implicit transactions are forbidden."""

    normalized = mode.upper()
    if normalized not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
        raise ValueError("unsupported transaction mode")
    if connection.in_transaction:
        raise StorageError("nested transactions are not supported")
    query_only = connection.execute("PRAGMA query_only").fetchone()
    if query_only is not None and int(query_only[0]) != 0:
        raise StorageReadOnlyError("database is in safe read-only mode")
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
    fault_injector: Callable[[str], None] | None = None,
) -> int:
    """Apply ordered migrations statement-by-statement in rollback-safe transactions."""

    if connection.in_transaction:
        raise MigrationError("migrations require an idle connection")
    connection.create_function(
        "sha256_utf8",
        1,
        lambda value: hashlib.sha256(str(value).encode("utf-8")).hexdigest(),
        deterministic=True,
    )
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
                for index, statement in enumerate(migration.statements):
                    if fault_injector is not None:
                        fault_injector(f"before_v{migration.version}_statement_{index}")
                    # Do not replace this with executescript(): sqlite3 executescript() performs
                    # an implicit commit and would make a failed migration non-atomic.
                    connection.execute(statement)
                    if fault_injector is not None:
                        fault_injector(f"after_v{migration.version}_statement_{index}")
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, migration.applied_at),
                )
                current = migration.version
            if fault_injector is not None:
                fault_injector("before_migration_commit")
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


def _verified_backup_version(path: Path) -> int:
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise MigrationError("recovery backup integrity check failed")
            return _current_version(connection)
    except sqlite3.Error as exc:
        raise MigrationError("recovery backup verification failed") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
