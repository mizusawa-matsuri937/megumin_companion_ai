"""Initial durable history, memory, profile, feature, and FTS schema."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    applied_at: str
    statements: tuple[str, ...]


_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE conversation_messages (
        message_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        origin TEXT NOT NULL CHECK (
            origin IN ('user_text', 'user_voice', 'assistant_dialogue', 'assistant_proactive')
        ),
        content TEXT NOT NULL CHECK (length(trim(content)) > 0),
        created_at TEXT NOT NULL,
        CHECK (
            (role = 'user' AND origin IN ('user_text', 'user_voice')) OR
            (role = 'assistant' AND origin IN ('assistant_dialogue', 'assistant_proactive'))
        ),
        UNIQUE (turn_id, role)
    ) STRICT
    """,
    """
    CREATE INDEX conversation_recent_idx
    ON conversation_messages(user_id, session_id, created_at DESC)
    """,
    """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        memory_id TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL,
        memory_type TEXT NOT NULL CHECK (
            memory_type IN ('user_profile', 'fact', 'event', 'emotion', 'relationship')
        ),
        canonical_key TEXT NOT NULL,
        content TEXT NOT NULL CHECK (length(trim(content)) > 0),
        normalized_content TEXT NOT NULL CHECK (length(normalized_content) > 0),
        importance_score REAL NOT NULL CHECK (importance_score BETWEEN 0.0 AND 1.0),
        confidence_score REAL NOT NULL CHECK (confidence_score BETWEEN 0.0 AND 1.0),
        sensitivity TEXT NOT NULL CHECK (
            sensitivity IN ('normal', 'health', 'address', 'other_sensitive')
        ),
        status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
        source_kind TEXT NOT NULL CHECK (source_kind IN ('dialogue', 'manual')),
        source_message_id TEXT NOT NULL,
        source_excerpt TEXT NOT NULL,
        related_emotion TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE (user_id, canonical_key, normalized_content)
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX one_active_memory_per_key_idx
    ON memories(user_id, canonical_key) WHERE status = 'active'
    """,
    """
    CREATE INDEX memory_listing_idx
    ON memories(user_id, status, importance_score DESC, updated_at DESC)
    """,
    """
    CREATE TABLE memory_sources (
        source_id TEXT PRIMARY KEY,
        memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
        source_kind TEXT NOT NULL CHECK (source_kind IN ('dialogue', 'manual')),
        source_message_id TEXT NOT NULL,
        source_excerpt TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        UNIQUE (memory_id, source_message_id, source_excerpt)
    ) STRICT
    """,
    """
    CREATE TABLE user_profiles (
        user_id TEXT NOT NULL,
        profile_key TEXT NOT NULL,
        memory_id TEXT NOT NULL UNIQUE REFERENCES memories(memory_id) ON DELETE CASCADE,
        value TEXT NOT NULL CHECK (length(trim(value)) > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, profile_key)
    ) STRICT
    """,
    """
    CREATE TABLE feature_flags (
        name TEXT PRIMARY KEY CHECK (
            name IN ('recent_history', 'long_term_memory', 'vision', 'cloud_vision', 'proactive')
        ),
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    INSERT INTO feature_flags(name, enabled, updated_at) VALUES
        ('recent_history', 1, '1970-01-01T00:00:00.000000Z'),
        ('long_term_memory', 0, '1970-01-01T00:00:00.000000Z'),
        ('vision', 0, '1970-01-01T00:00:00.000000Z'),
        ('cloud_vision', 0, '1970-01-01T00:00:00.000000Z'),
        ('proactive', 0, '1970-01-01T00:00:00.000000Z')
    """,
    """
    CREATE VIRTUAL TABLE memories_fts USING fts5(
        content,
        canonical_key,
        source_excerpt,
        content = 'memories',
        content_rowid = 'id',
        tokenize = 'trigram'
    )
    """,
    "INSERT INTO memories_fts(memories_fts, rank) VALUES ('secure-delete', 1)",
    """
    CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, canonical_key, source_excerpt)
        VALUES (new.id, new.content, new.canonical_key, new.source_excerpt);
    END
    """,
    """
    CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, canonical_key, source_excerpt)
        VALUES ('delete', old.id, old.content, old.canonical_key, old.source_excerpt);
    END
    """,
    """
    CREATE TRIGGER memories_fts_update AFTER UPDATE OF content, canonical_key, source_excerpt
    ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, canonical_key, source_excerpt)
        VALUES ('delete', old.id, old.content, old.canonical_key, old.source_excerpt);
        INSERT INTO memories_fts(rowid, content, canonical_key, source_excerpt)
        VALUES (new.id, new.content, new.canonical_key, new.source_excerpt);
    END
    """,
)

MIGRATIONS = (
    Migration(
        version=1,
        name="initial_history_memory_features_fts",
        applied_at="2026-07-13T00:00:00.000000Z",
        statements=_STATEMENTS,
    ),
)
