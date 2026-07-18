"""Add body-free atomic message idempotency and persistent session ownership."""

from __future__ import annotations

from app.storage.migrations.v001_initial import Migration

MIGRATION = Migration(
    version=2,
    name="message_idempotency",
    applied_at="2026-07-18T00:00:00.000000Z",
    statements=(
        """
        CREATE TABLE idempotency_sessions (
            client_id TEXT NOT NULL CHECK (length(client_id) BETWEEN 1 AND 128),
            session_id TEXT NOT NULL UNIQUE CHECK (length(session_id) BETWEEN 1 AND 128),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminal_sequence INTEGER NOT NULL DEFAULT 0 CHECK (terminal_sequence >= 0),
            PRIMARY KEY (client_id, session_id)
        ) STRICT
        """,
        """
        CREATE TABLE idempotency_turns (
            id INTEGER PRIMARY KEY,
            client_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
            fingerprint TEXT NOT NULL CHECK (
                length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            turn_id TEXT NOT NULL UNIQUE CHECK (length(turn_id) BETWEEN 1 AND 128),
            input_mode TEXT NOT NULL CHECK (input_mode IN ('text', 'voice')),
            status TEXT NOT NULL CHECK (
                status IN ('accepted', 'streaming', 'speaking', 'completed', 'cancelled', 'failed')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error_code TEXT,
            terminal_order INTEGER CHECK (terminal_order IS NULL OR terminal_order > 0),
            UNIQUE (client_id, session_id, message_id),
            FOREIGN KEY (client_id, session_id)
                REFERENCES idempotency_sessions(client_id, session_id) ON DELETE CASCADE
        ) STRICT
        """,
        """
        CREATE INDEX idempotency_terminal_retention_idx
        ON idempotency_turns(client_id, session_id, status, terminal_order DESC)
        """,
    ),
)
