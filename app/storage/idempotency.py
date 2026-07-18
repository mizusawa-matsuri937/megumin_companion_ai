"""Async SQLite adapter for atomic, body-free message idempotency."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.idempotency import (
    DEFAULT_TERMINAL_LIMIT,
    DEFAULT_TERMINAL_TTL,
    TERMINAL_STATUSES,
    IdempotencyAccessError,
    IdempotencyClaim,
    IdempotencyConflictError,
    IdempotencyKey,
    IdempotencyUnavailableError,
    RetainedTurn,
    validate_fingerprint,
    validate_state_transition,
)
from app.schemas import InputMode, TurnState, TurnStatus
from app.storage.database import SQLiteDatabase, transaction


class SQLiteIdempotencyStore:
    """Serialize claims with SQLite's write lock before any paid or visible side effect."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        terminal_limit: int = DEFAULT_TERMINAL_LIMIT,
        terminal_ttl: timedelta = DEFAULT_TERMINAL_TTL,
    ) -> None:
        if (
            not 1 <= terminal_limit <= DEFAULT_TERMINAL_LIMIT
            or not timedelta(0) < terminal_ttl <= DEFAULT_TERMINAL_TTL
        ):
            raise ValueError("idempotency retention bounds must not exceed hard limits")
        self._database = database
        self._terminal_limit = terminal_limit
        self._terminal_ttl = terminal_ttl

    async def bind_session(self, client_id: str, session_id: str, *, now: datetime) -> None:
        await self._offload(self._bind_session_sync, client_id, session_id, now)

    async def claim(
        self,
        key: IdempotencyKey,
        *,
        fingerprint: str,
        state: TurnState,
        now: datetime,
    ) -> IdempotencyClaim:
        result = await self._offload(self._claim_sync, key, fingerprint, state, now)
        assert isinstance(result, IdempotencyClaim)
        return result

    async def update(
        self,
        *,
        client_id: str,
        state: TurnState,
        now: datetime,
    ) -> None:
        await self._offload(self._update_sync, client_id, state, now)

    async def lookup_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> TurnState | None:
        result = await self._offload(self._lookup_turn_sync, client_id, session_id, turn_id, now)
        assert result is None or isinstance(result, TurnState)
        return result

    async def list_session(
        self,
        *,
        client_id: str,
        session_id: str,
        now: datetime,
    ) -> tuple[RetainedTurn, ...]:
        result = await self._offload(self._list_session_sync, client_id, session_id, now)
        assert isinstance(result, tuple)
        return result

    async def recover_incomplete(self, *, now: datetime) -> int:
        result = await self._offload(self._recover_incomplete_sync, now)
        assert isinstance(result, int)
        return result

    async def count_records(self, *, client_id: str, session_id: str) -> int:
        result = await self._offload(self._count_records_sync, client_id, session_id)
        assert isinstance(result, int)
        return result

    async def _offload(self, operation: Any, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args)
        except (IdempotencyAccessError, IdempotencyConflictError):
            raise
        except Exception as exc:
            raise IdempotencyUnavailableError from exc

    def _bind_session_sync(self, client_id: str, session_id: str, now: datetime) -> None:
        timestamp = _to_db_time(now)
        with self._database.connect() as connection, transaction(connection):
            self._bind_session(connection, client_id, session_id, timestamp)
            self._prune(connection, client_id, session_id, now=now)

    def _claim_sync(
        self,
        key: IdempotencyKey,
        fingerprint: str,
        state: TurnState,
        now: datetime,
    ) -> IdempotencyClaim:
        validate_fingerprint(fingerprint)
        if (
            state.status is not TurnStatus.accepted
            or state.session_id != key.session_id
            or state.source_message_id != key.message_id
        ):
            raise IdempotencyConflictError
        timestamp = _to_db_time(now)
        with self._database.connect() as connection, transaction(connection):
            self._bind_session(connection, key.client_id, key.session_id, timestamp)
            self._prune(connection, key.client_id, key.session_id, now=now)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO idempotency_turns(
                    client_id, session_id, message_id, fingerprint, turn_id, input_mode,
                    status, created_at, updated_at, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key.client_id,
                    key.session_id,
                    key.message_id,
                    fingerprint,
                    state.turn_id,
                    state.input_mode.value,
                    state.status.value,
                    _to_db_time(state.created_at),
                    _to_db_time(state.updated_at),
                    state.error_code,
                ),
            )
            if cursor.rowcount == 1:
                return IdempotencyClaim(state=state, created=True)
            row = connection.execute(
                """
                SELECT * FROM idempotency_turns
                WHERE client_id = ? AND session_id = ? AND message_id = ?
                """,
                key.as_tuple(),
            ).fetchone()
            if row is None or str(row["fingerprint"]) != fingerprint:
                raise IdempotencyConflictError
            existing = _state_from_row(row)
            if existing.status in TERMINAL_STATUSES:
                self._touch_terminal(connection, key.client_id, key.session_id, existing.turn_id)
            return IdempotencyClaim(state=existing, created=False)

    def _update_sync(self, client_id: str, state: TurnState, now: datetime) -> None:
        _to_db_time(now)
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(
                "SELECT * FROM idempotency_turns WHERE turn_id = ?", (state.turn_id,)
            ).fetchone()
            if row is None:
                raise IdempotencyConflictError
            if client_id != str(row["client_id"]) or state.session_id != str(row["session_id"]):
                raise IdempotencyAccessError
            if state.source_message_id != str(row["message_id"]):
                raise IdempotencyConflictError
            validate_state_transition(TurnStatus(str(row["status"])), state.status)
            terminal_order = row["terminal_order"]
            if (
                TurnStatus(str(row["status"])) not in TERMINAL_STATUSES
                and state.status in TERMINAL_STATUSES
            ):
                terminal_order = self._next_terminal_order(connection, client_id, state.session_id)
            connection.execute(
                """
                UPDATE idempotency_turns
                SET status = ?, updated_at = ?, error_code = ?, terminal_order = ?
                WHERE turn_id = ?
                """,
                (
                    state.status.value,
                    _to_db_time(state.updated_at),
                    state.error_code,
                    terminal_order,
                    state.turn_id,
                ),
            )
            self._prune(connection, client_id, state.session_id, now=now)

    def _lookup_turn_sync(
        self,
        client_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> TurnState | None:
        with self._database.connect() as connection, transaction(connection):
            self._assert_session_access(connection, client_id, session_id)
            self._prune(connection, client_id, session_id, now=now)
            row = connection.execute(
                "SELECT * FROM idempotency_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                return None
            if client_id != str(row["client_id"]) or session_id != str(row["session_id"]):
                raise IdempotencyAccessError
            state = _state_from_row(row)
            if state.status in TERMINAL_STATUSES:
                self._touch_terminal(connection, client_id, session_id, turn_id)
            return state

    def _recover_incomplete_sync(self, now: datetime) -> int:
        timestamp = _to_db_time(now)
        with self._database.connect() as connection, transaction(connection):
            incomplete = connection.execute(
                """
                SELECT turn_id, client_id, session_id FROM idempotency_turns
                WHERE status NOT IN ('completed', 'cancelled', 'failed') ORDER BY id
                """
            ).fetchall()
            for row in incomplete:
                order = self._next_terminal_order(
                    connection,
                    str(row["client_id"]),
                    str(row["session_id"]),
                )
                connection.execute(
                    """
                    UPDATE idempotency_turns
                    SET status = 'failed', updated_at = ?, error_code = 'service_restarted',
                        terminal_order = ?
                    WHERE turn_id = ?
                    """,
                    (timestamp, order, str(row["turn_id"])),
                )
            sessions = connection.execute(
                "SELECT client_id, session_id FROM idempotency_sessions"
            ).fetchall()
            for session in sessions:
                self._prune(
                    connection,
                    str(session["client_id"]),
                    str(session["session_id"]),
                    now=now,
                )
            return len(incomplete)

    def _list_session_sync(
        self,
        client_id: str,
        session_id: str,
        now: datetime,
    ) -> tuple[RetainedTurn, ...]:
        with self._database.connect() as connection, transaction(connection):
            self._assert_session_access(connection, client_id, session_id)
            self._prune(connection, client_id, session_id, now=now)
            rows = connection.execute(
                """
                SELECT * FROM idempotency_turns
                WHERE client_id = ? AND session_id = ?
                ORDER BY created_at, turn_id
                """,
                (client_id, session_id),
            ).fetchall()
            return tuple(
                RetainedTurn(
                    state=_state_from_row(row),
                    terminal_order=(
                        int(row["terminal_order"]) if row["terminal_order"] is not None else None
                    ),
                )
                for row in rows
            )

    def _count_records_sync(self, client_id: str, session_id: str) -> int:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM idempotency_turns
                WHERE client_id = ? AND session_id = ?
                """,
                (client_id, session_id),
            ).fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _bind_session(
        connection: sqlite3.Connection,
        client_id: str,
        session_id: str,
        timestamp: str,
    ) -> None:
        if not 1 <= len(client_id) <= 128 or not 1 <= len(session_id) <= 128:
            raise ValueError("session identity must contain 1 to 128 characters")
        connection.execute(
            """
            INSERT OR IGNORE INTO idempotency_sessions(
                client_id, session_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (client_id, session_id, timestamp, timestamp),
        )
        row = connection.execute(
            "SELECT client_id FROM idempotency_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None or str(row["client_id"]) != client_id:
            raise IdempotencyAccessError
        connection.execute(
            """
            UPDATE idempotency_sessions SET updated_at = ?
            WHERE client_id = ? AND session_id = ?
            """,
            (timestamp, client_id, session_id),
        )

    @staticmethod
    def _assert_session_access(
        connection: sqlite3.Connection,
        client_id: str,
        session_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT client_id FROM idempotency_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is not None and str(row["client_id"]) != client_id:
            raise IdempotencyAccessError

    @staticmethod
    def _next_terminal_order(
        connection: sqlite3.Connection,
        client_id: str,
        session_id: str,
    ) -> int:
        connection.execute(
            """
            UPDATE idempotency_sessions
            SET terminal_sequence = terminal_sequence + 1
            WHERE client_id = ? AND session_id = ?
            """,
            (client_id, session_id),
        )
        row = connection.execute(
            """
            SELECT terminal_sequence FROM idempotency_sessions
            WHERE client_id = ? AND session_id = ?
            """,
            (client_id, session_id),
        ).fetchone()
        if row is None:
            raise IdempotencyConflictError
        return int(row["terminal_sequence"])

    def _touch_terminal(
        self,
        connection: sqlite3.Connection,
        client_id: str,
        session_id: str,
        turn_id: str,
    ) -> None:
        order = self._next_terminal_order(connection, client_id, session_id)
        connection.execute(
            "UPDATE idempotency_turns SET terminal_order = ? WHERE turn_id = ?",
            (order, turn_id),
        )

    def _prune(
        self,
        connection: sqlite3.Connection,
        client_id: str,
        session_id: str,
        *,
        now: datetime,
    ) -> None:
        cutoff = _to_db_time(now.astimezone(UTC) - self._terminal_ttl)
        terminal_values = tuple(status.value for status in TERMINAL_STATUSES)
        placeholders = ", ".join("?" for _status in terminal_values)
        connection.execute(
            f"""
            DELETE FROM idempotency_turns
            WHERE client_id = ? AND session_id = ?
              AND status IN ({placeholders}) AND updated_at < ?
            """,
            (client_id, session_id, *terminal_values, cutoff),
        )
        connection.execute(
            f"""
            DELETE FROM idempotency_turns WHERE id IN (
                SELECT id FROM idempotency_turns
                WHERE client_id = ? AND session_id = ? AND status IN ({placeholders})
                ORDER BY terminal_order DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (client_id, session_id, *terminal_values, self._terminal_limit),
        )


def _state_from_row(row: sqlite3.Row) -> TurnState:
    return TurnState(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        source_message_id=str(row["message_id"]),
        input_mode=InputMode(str(row["input_mode"])),
        status=TurnStatus(str(row["status"])),
        created_at=_from_db_time(str(row["created_at"])),
        updated_at=_from_db_time(str(row["updated_at"])),
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
    )


def _to_db_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("idempotency timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _from_db_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
