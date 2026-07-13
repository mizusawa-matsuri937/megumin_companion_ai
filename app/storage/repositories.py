"""Synchronous SQLite repositories; async callers should dispatch them off-loop."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.emotion.models import EmotionLabel
from app.memory.models import (
    ApprovedMemory,
    MemoryItem,
    MemorySensitivity,
    MemorySource,
    MemorySourceKind,
    MemoryStatus,
    MemoryType,
    ProfileItem,
)
from app.schemas.ai import FeatureName, FeatureState
from app.storage.database import SQLiteDatabase, StorageConflictError, transaction
from app.storage.records import ConversationOrigin, ConversationRecord, ConversationRole


class ConversationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def add(self, record: ConversationRecord) -> bool:
        with self._database.connect() as connection, transaction(connection):
            try:
                connection.execute(
                    """
                    INSERT INTO conversation_messages(
                        message_id, session_id, user_id, turn_id, role, origin, content, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.message_id,
                        record.session_id,
                        record.user_id,
                        record.turn_id,
                        record.role.value,
                        record.origin.value,
                        record.content,
                        _to_db_time(record.created_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = connection.execute(
                    "SELECT * FROM conversation_messages WHERE message_id = ?",
                    (record.message_id,),
                ).fetchone()
                if existing is not None and _conversation_from_row(existing) == record:
                    return False
                raise StorageConflictError(
                    "conversation id or turn role was reused with different content"
                ) from exc
        return True

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
        if retention_days < 1 or limit < 1:
            raise ValueError("retention_days and limit must be positive")
        cutoff = _to_db_time(_aware(now) - timedelta(days=retention_days))
        exclusion = exclude_message_id or ""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM conversation_messages
                    WHERE user_id = ? AND session_id = ? AND created_at >= ?
                      AND message_id != ?
                    ORDER BY created_at DESC, message_id DESC
                    LIMIT ?
                ) ORDER BY created_at ASC, message_id ASC
                """,
                (user_id, session_id, cutoff, exclusion, limit),
            ).fetchall()
        return [_conversation_from_row(row) for row in rows]

    def cleanup_expired(self, *, now: datetime, retention_days: int = 7) -> int:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        cutoff = _to_db_time(_aware(now) - timedelta(days=retention_days))
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "DELETE FROM conversation_messages WHERE created_at < ?", (cutoff,)
            )
            deleted = cursor.rowcount
        if deleted:
            self._database.secure_cleanup()
        return deleted

    def clear(self, *, user_id: str, session_id: str | None = None) -> int:
        with self._database.connect() as connection, transaction(connection):
            if session_id is None:
                cursor = connection.execute(
                    "DELETE FROM conversation_messages WHERE user_id = ?", (user_id,)
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM conversation_messages WHERE user_id = ? AND session_id = ?",
                    (user_id, session_id),
                )
            deleted = cursor.rowcount
        if deleted:
            self._database.secure_cleanup(vacuum=True)
        return deleted


class FeatureFlagRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def get(self, name: FeatureName) -> FeatureState:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT name, enabled FROM feature_flags WHERE name = ?", (name.value,)
            ).fetchone()
        if row is None:
            raise KeyError(name.value)
        return FeatureState(name=FeatureName(str(row["name"])), enabled=bool(row["enabled"]))

    def list(self) -> list[FeatureState]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT name, enabled FROM feature_flags ORDER BY name"
            ).fetchall()
        return [
            FeatureState(name=FeatureName(str(row["name"])), enabled=bool(row["enabled"]))
            for row in rows
        ]

    def set(self, name: FeatureName, enabled: bool, *, updated_at: datetime) -> FeatureState:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE feature_flags SET enabled = ?, updated_at = ? WHERE name = ?",
                (int(enabled), _to_db_time(_aware(updated_at)), name.value),
            )
            if cursor.rowcount != 1:
                raise KeyError(name.value)
        return FeatureState(name=name, enabled=enabled)


class MemoryRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def upsert(self, approved: ApprovedMemory) -> MemoryItem:
        now = _to_db_time(approved.created_at)
        with self._database.connect() as connection, transaction(connection):
            exact = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND canonical_key = ? AND normalized_content = ?
                """,
                (approved.user_id, approved.canonical_key, approved.normalized_content),
            ).fetchone()
            connection.execute(
                """
                UPDATE memories SET status = 'superseded', updated_at = ?
                WHERE user_id = ? AND canonical_key = ? AND status = 'active'
                  AND normalized_content != ?
                """,
                (now, approved.user_id, approved.canonical_key, approved.normalized_content),
            )
            if exact is None:
                connection.execute(
                    """
                    INSERT INTO memories(
                        memory_id, user_id, memory_type, canonical_key, content,
                        normalized_content, importance_score, confidence_score, sensitivity,
                        status, source_kind, source_message_id, source_excerpt, related_emotion,
                        created_at, updated_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approved.memory_id,
                        approved.user_id,
                        approved.memory_type.value,
                        approved.canonical_key,
                        approved.content,
                        approved.normalized_content,
                        approved.importance_score,
                        approved.confidence_score,
                        approved.sensitivity.value,
                        approved.source_kind.value,
                        approved.source_message_id,
                        approved.source_excerpt,
                        approved.related_emotion.value if approved.related_emotion else None,
                        now,
                        now,
                        now,
                    ),
                )
                memory_id = approved.memory_id
                created_at = now
            else:
                memory_id = str(exact["memory_id"])
                created_at = str(exact["created_at"])
                if str(exact["memory_type"]) != approved.memory_type.value:
                    raise StorageConflictError("canonical memory key changed type")
                connection.execute(
                    """
                    UPDATE memories SET
                        content = ?, importance_score = MAX(importance_score, ?),
                        confidence_score = MAX(confidence_score, ?), sensitivity = ?,
                        status = 'active', source_kind = ?, source_message_id = ?,
                        source_excerpt = ?, related_emotion = ?, updated_at = ?, last_seen_at = ?
                    WHERE memory_id = ?
                    """,
                    (
                        approved.content,
                        approved.importance_score,
                        approved.confidence_score,
                        approved.sensitivity.value,
                        approved.source_kind.value,
                        approved.source_message_id,
                        approved.source_excerpt,
                        approved.related_emotion.value if approved.related_emotion else None,
                        now,
                        now,
                        memory_id,
                    ),
                )
            self._add_source(connection, memory_id, approved)
            if approved.memory_type is MemoryType.user_profile:
                connection.execute(
                    """
                    INSERT INTO user_profiles(
                        user_id, profile_key, memory_id, value, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, profile_key) DO UPDATE SET
                        memory_id = excluded.memory_id,
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (
                        approved.user_id,
                        approved.canonical_key,
                        memory_id,
                        approved.content,
                        created_at,
                        now,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            assert row is not None
            return _memory_from_row(row)

    def get(self, memory_id: str, *, user_id: str | None = None) -> MemoryItem | None:
        with self._database.connect() as connection:
            if user_id is None:
                row = connection.execute(
                    "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM memories WHERE memory_id = ? AND user_id = ?",
                    (memory_id, user_id),
                ).fetchone()
        return _memory_from_row(row) if row is not None else None

    def list_items(self, *, user_id: str, include_superseded: bool = False) -> list[MemoryItem]:
        status_clause = "" if include_superseded else "AND status = 'active'"
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memories WHERE user_id = ? {status_clause}
                ORDER BY importance_score DESC, updated_at DESC, memory_id ASC
                """,
                (user_id,),
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def search(self, *, user_id: str, query: str, limit: int = 10) -> list[MemoryItem]:
        normalized_query = " ".join(query.split())
        if limit < 1:
            raise ValueError("limit must be positive")
        if not normalized_query:
            return self.list_items(user_id=user_id)[:limit]
        with self._database.connect() as connection:
            if len(normalized_query) >= 3:
                quoted = '"' + normalized_query.replace('"', '""') + '"'
                rows = connection.execute(
                    """
                    SELECT memories.* FROM memories_fts
                    JOIN memories ON memories.id = memories_fts.rowid
                    WHERE memories_fts MATCH ? AND memories.user_id = ?
                      AND memories.status = 'active'
                    ORDER BY bm25(memories_fts), memories.importance_score DESC
                    LIMIT ?
                    """,
                    (quoted, user_id, limit),
                ).fetchall()
            else:
                rows = self._like_search(connection, user_id, normalized_query, limit)
        return [_memory_from_row(row) for row in rows]

    def list_sources(self, memory_id: str) -> list[MemorySource]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_sources WHERE memory_id = ? ORDER BY observed_at, source_id",
                (memory_id,),
            ).fetchall()
        return [_source_from_row(row) for row in rows]

    def list_profiles(self, *, user_id: str) -> list[ProfileItem]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id = ? ORDER BY profile_key", (user_id,)
            ).fetchall()
        return [_profile_from_row(row) for row in rows]

    def update_content(
        self,
        memory_id: str,
        *,
        user_id: str,
        content: str,
        normalized_content: str,
        updated_at: datetime,
    ) -> MemoryItem | None:
        now = _to_db_time(updated_at)
        with self._database.connect() as connection, transaction(connection):
            existing = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ? AND user_id = ?",
                (memory_id, user_id),
            ).fetchone()
            if existing is None:
                return None
            sensitivity = _storable_sensitivity(content)
            source_message_id = f"manual_edit:{now}"
            try:
                connection.execute(
                    """
                    UPDATE memories SET
                        content = ?, normalized_content = ?, confidence_score = 1.0,
                        sensitivity = ?, source_kind = 'manual', source_message_id = ?,
                        source_excerpt = ?, updated_at = ?, last_seen_at = ?
                    WHERE memory_id = ?
                    """,
                    (
                        content,
                        normalized_content,
                        sensitivity.value,
                        source_message_id,
                        content,
                        now,
                        now,
                        memory_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError("edited memory duplicates an existing value") from exc
            connection.execute(
                "UPDATE user_profiles SET value = ?, updated_at = ? WHERE memory_id = ?",
                (content, now, memory_id),
            )
            connection.execute(
                """
                INSERT INTO memory_sources(
                    source_id, memory_id, source_kind, source_message_id,
                    source_excerpt, observed_at
                ) VALUES (?, ?, 'manual', ?, ?, ?)
                """,
                (f"source_{uuid4().hex}", memory_id, source_message_id, content, now),
            )
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            assert row is not None
            return _memory_from_row(row)

    def delete(self, memory_id: str, *, user_id: str | None = None) -> bool:
        with self._database.connect() as connection, transaction(connection):
            if user_id is None:
                cursor = connection.execute(
                    "DELETE FROM memories WHERE memory_id = ?", (memory_id,)
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM memories WHERE memory_id = ? AND user_id = ?",
                    (memory_id, user_id),
                )
            deleted = cursor.rowcount == 1
        if deleted:
            self._database.secure_cleanup()
        return deleted

    def clear(self, *, user_id: str) -> int:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
            deleted = cursor.rowcount
        if deleted:
            self._database.secure_cleanup(vacuum=True)
        return deleted

    @staticmethod
    def _add_source(
        connection: sqlite3.Connection, memory_id: str, approved: ApprovedMemory
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_sources(
                source_id, memory_id, source_kind, source_message_id, source_excerpt, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"source_{uuid4().hex}",
                memory_id,
                approved.source_kind.value,
                approved.source_message_id,
                approved.source_excerpt,
                _to_db_time(approved.created_at),
            ),
        )

    @staticmethod
    def _like_search(
        connection: sqlite3.Connection, user_id: str, query: str, limit: int
    ) -> list[sqlite3.Row]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return connection.execute(
            """
            SELECT * FROM memories
            WHERE user_id = ? AND status = 'active'
              AND (content LIKE ? ESCAPE '\\' OR canonical_key LIKE ? ESCAPE '\\')
            ORDER BY importance_score DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, f"%{escaped}%", f"%{escaped}%", limit),
        ).fetchall()


def _conversation_from_row(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        message_id=str(row["message_id"]),
        session_id=str(row["session_id"]),
        user_id=str(row["user_id"]),
        turn_id=str(row["turn_id"]),
        role=ConversationRole(str(row["role"])),
        origin=ConversationOrigin(str(row["origin"])),
        content=str(row["content"]),
        created_at=_from_db_time(str(row["created_at"])),
    )


def _memory_from_row(row: sqlite3.Row) -> MemoryItem:
    related = row["related_emotion"]
    return MemoryItem(
        memory_id=str(row["memory_id"]),
        user_id=str(row["user_id"]),
        memory_type=MemoryType(str(row["memory_type"])),
        canonical_key=str(row["canonical_key"]),
        content=str(row["content"]),
        normalized_content=str(row["normalized_content"]),
        importance_score=float(row["importance_score"]),
        confidence_score=float(row["confidence_score"]),
        sensitivity=MemorySensitivity(str(row["sensitivity"])),
        status=MemoryStatus(str(row["status"])),
        source_kind=MemorySourceKind(str(row["source_kind"])),
        source_message_id=str(row["source_message_id"]),
        source_excerpt=str(row["source_excerpt"]),
        related_emotion=EmotionLabel(str(related)) if related is not None else None,
        created_at=_from_db_time(str(row["created_at"])),
        updated_at=_from_db_time(str(row["updated_at"])),
        last_seen_at=_from_db_time(str(row["last_seen_at"])),
    )


def _source_from_row(row: sqlite3.Row) -> MemorySource:
    return MemorySource(
        source_id=str(row["source_id"]),
        memory_id=str(row["memory_id"]),
        source_kind=MemorySourceKind(str(row["source_kind"])),
        source_message_id=str(row["source_message_id"]),
        source_excerpt=str(row["source_excerpt"]),
        observed_at=_from_db_time(str(row["observed_at"])),
    )


def _profile_from_row(row: sqlite3.Row) -> ProfileItem:
    return ProfileItem(
        user_id=str(row["user_id"]),
        profile_key=str(row["profile_key"]),
        memory_id=str(row["memory_id"]),
        value=str(row["value"]),
        created_at=_from_db_time(str(row["created_at"])),
        updated_at=_from_db_time(str(row["updated_at"])),
    )


def _to_db_time(value: datetime) -> str:
    return _aware(value).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _from_db_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("storage timestamps must be timezone-aware")
    return value


def _storable_sensitivity(content: str) -> MemorySensitivity:
    # Import locally so storage remains an infrastructure adapter rather than policy owner.
    from app.memory.privacy import classify_sensitivity

    sensitivity = classify_sensitivity(content)
    if sensitivity is MemorySensitivity.credential:
        raise ValueError("credential content cannot be persisted")
    return sensitivity
