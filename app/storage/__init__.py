"""SQLite boundary and repositories for private local state."""

from app.storage.database import (
    DatabaseAccessMode,
    DatabaseSafeModeError,
    DatabaseStatus,
    MigrationError,
    RecoveryChoice,
    SQLiteDatabase,
    StorageConflictError,
    StorageError,
    StorageReadOnlyError,
)
from app.storage.idempotency import SQLiteIdempotencyStore
from app.storage.records import (
    CleanupJob,
    CleanupKind,
    CleanupState,
    ConversationOrigin,
    ConversationRecord,
    ConversationRole,
    DeletionResult,
)
from app.storage.repositories import (
    ConversationRepository,
    FeatureFlagRepository,
    MemoryRepository,
    PhysicalCleanupRepository,
)

__all__ = [
    "CleanupJob",
    "CleanupKind",
    "CleanupState",
    "ConversationOrigin",
    "ConversationRecord",
    "ConversationRepository",
    "ConversationRole",
    "DeletionResult",
    "DatabaseAccessMode",
    "DatabaseSafeModeError",
    "DatabaseStatus",
    "FeatureFlagRepository",
    "MemoryRepository",
    "MigrationError",
    "PhysicalCleanupRepository",
    "RecoveryChoice",
    "SQLiteDatabase",
    "SQLiteIdempotencyStore",
    "StorageConflictError",
    "StorageError",
    "StorageReadOnlyError",
]
