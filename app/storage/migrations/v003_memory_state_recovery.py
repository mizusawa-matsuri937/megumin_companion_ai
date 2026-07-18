"""Add recoverable feature state, minimal evidence, and cleanup tracking."""

from __future__ import annotations

from app.storage.migrations.v001_initial import Migration

MIGRATION = Migration(
    version=3,
    name="memory_state_recovery",
    applied_at="2026-07-18T00:00:00.000000Z",
    statements=(
        """
        CREATE TABLE feature_flags_v3 (
            name TEXT PRIMARY KEY CHECK (
                name IN (
                    'recent_history', 'long_term_memory', 'vision', 'cloud_vision', 'proactive'
                )
            ),
            desired_state TEXT NOT NULL CHECK (desired_state IN ('disabled', 'enabled')),
            actual_state TEXT NOT NULL CHECK (
                actual_state IN ('disabled', 'enabling', 'enabled', 'disabling', 'failed')
            ),
            generation INTEGER NOT NULL CHECK (generation >= 0),
            reason_code TEXT CHECK (
                reason_code IS NULL OR (
                    length(reason_code) BETWEEN 1 AND 64 AND
                    substr(reason_code, 1, 1) GLOB '[a-z]' AND
                    reason_code NOT GLOB '*[^a-z0-9_]*'
                )
            ),
            updated_at TEXT NOT NULL
        ) STRICT
        """,
        """
        INSERT INTO feature_flags_v3(
            name, desired_state, actual_state, generation, reason_code, updated_at
        )
        SELECT name,
               CASE enabled WHEN 1 THEN 'enabled' ELSE 'disabled' END,
               CASE enabled WHEN 1 THEN 'enabled' ELSE 'disabled' END,
               0, NULL, updated_at
        FROM feature_flags
        """,
        "DROP TABLE feature_flags",
        "ALTER TABLE feature_flags_v3 RENAME TO feature_flags",
        """
        ALTER TABLE memories ADD COLUMN source_sha256 TEXT NOT NULL
        DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'
        CHECK (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*')
        """,
        """
        ALTER TABLE memories ADD COLUMN provenance TEXT NOT NULL
        DEFAULT 'legacy_unverified'
        CHECK (provenance IN ('dialogue_text', 'dialogue_voice', 'manual', 'legacy_unverified'))
        """,
        """
        ALTER TABLE memory_sources ADD COLUMN source_sha256 TEXT NOT NULL
        DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'
        CHECK (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*')
        """,
        """
        ALTER TABLE memory_sources ADD COLUMN provenance TEXT NOT NULL
        DEFAULT 'legacy_unverified'
        CHECK (provenance IN ('dialogue_text', 'dialogue_voice', 'manual', 'legacy_unverified'))
        """,
        """
        UPDATE memories
        SET source_sha256 = sha256_utf8(source_excerpt),
            provenance = 'legacy_unverified',
            source_excerpt = CASE
                WHEN instr(lower(source_excerpt), lower(content)) > 0
                THEN substr(content, 1, 512)
                ELSE '[legacy evidence redacted]'
            END
        """,
        """
        UPDATE memory_sources
        SET source_sha256 = sha256_utf8(source_excerpt),
            provenance = 'legacy_unverified',
            source_excerpt = COALESCE(
                (
                    SELECT CASE
                        WHEN instr(
                            lower(memory_sources.source_excerpt), lower(memories.content)
                        ) > 0
                        THEN substr(memories.content, 1, 512)
                        ELSE '[legacy evidence redacted]'
                    END
                    FROM memories WHERE memories.memory_id = memory_sources.memory_id
                ),
                '[legacy evidence redacted]'
            )
        """,
        "DROP TRIGGER memories_fts_insert",
        "DROP TRIGGER memories_fts_delete",
        "DROP TRIGGER memories_fts_update",
        "DROP TABLE memories_fts",
        """
        CREATE VIRTUAL TABLE memories_fts USING fts5(
            content,
            canonical_key,
            content = 'memories',
            content_rowid = 'id',
            tokenize = 'trigram'
        )
        """,
        "INSERT INTO memories_fts(memories_fts, rank) VALUES ('secure-delete', 1)",
        """
        CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories
        WHEN new.status = 'active' BEGIN
            INSERT INTO memories_fts(rowid, content, canonical_key)
            VALUES (new.id, new.content, new.canonical_key);
        END
        """,
        """
        CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories
        WHEN old.status = 'active' BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, canonical_key)
            VALUES ('delete', old.id, old.content, old.canonical_key);
        END
        """,
        """
        CREATE TRIGGER memories_fts_update
        AFTER UPDATE OF content, canonical_key, status ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, canonical_key)
            SELECT 'delete', old.id, old.content, old.canonical_key
            WHERE old.status = 'active';
            INSERT INTO memories_fts(rowid, content, canonical_key)
            SELECT new.id, new.content, new.canonical_key
            WHERE new.status = 'active';
        END
        """,
        "INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')",
        """
        CREATE TABLE physical_cleanup_jobs (
            cleanup_id TEXT PRIMARY KEY CHECK (length(cleanup_id) BETWEEN 1 AND 128),
            kind TEXT NOT NULL CHECK (
                kind IN (
                    'memory_delete', 'memory_clear', 'history_clear',
                    'history_retention', 'evidence_migration'
                )
            ),
            state TEXT NOT NULL CHECK (state IN ('pending', 'retrying', 'completed')),
            vacuum_required INTEGER NOT NULL CHECK (vacuum_required IN (0, 1)),
            attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
            reason_code TEXT CHECK (
                reason_code IS NULL OR reason_code IN (
                    'db_busy', 'db_full', 'db_corrupt', 'file_locked', 'cleanup_failed'
                )
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            next_attempt_at TEXT NOT NULL,
            completed_at TEXT
        ) STRICT
        """,
        """
        CREATE INDEX physical_cleanup_due_idx
        ON physical_cleanup_jobs(state, next_attempt_at, created_at)
        """,
        """
        INSERT INTO physical_cleanup_jobs(
            cleanup_id, kind, state, vacuum_required, attempt_count, reason_code,
            created_at, updated_at, next_attempt_at, completed_at
        ) SELECT
            'cleanup_v3_evidence_migration', 'evidence_migration', 'pending', 1, 0, NULL,
            '2026-07-18T00:00:00.000000Z', '2026-07-18T00:00:00.000000Z',
            '1970-01-01T00:00:00.000000Z', NULL
        WHERE EXISTS (SELECT 1 FROM memories)
        """,
    ),
)
