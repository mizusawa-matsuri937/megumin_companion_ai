from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.core.idempotency import (
    IdempotencyAccessError,
    IdempotencyConflictError,
    IdempotencyKey,
    message_fingerprint,
)
from app.schemas import InputMode, TurnState, TurnStatus, UserMessage
from app.storage import SQLiteDatabase, SQLiteIdempotencyStore

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _state(message_id: str, *, turn_id: str, now: datetime = NOW) -> TurnState:
    return TurnState(
        turn_id=turn_id,
        session_id="session-w06",
        source_message_id=message_id,
        input_mode=InputMode.text,
        created_at=now,
        updated_at=now,
    )


def _key(message_id: str, *, client_id: str = "client-w06") -> IdempotencyKey:
    return IdempotencyKey(client_id, "session-w06", message_id)


def _store(tmp_path: Path) -> tuple[SQLiteDatabase, SQLiteIdempotencyStore]:
    database = SQLiteDatabase(tmp_path / "companion.sqlite3")
    database.initialize()
    return database, SQLiteIdempotencyStore(database)


def test_sqlite_claim_is_atomic_under_concurrent_duplicates(tmp_path: Path) -> None:
    _database, store = _store(tmp_path)
    message = UserMessage(
        message_id="message-concurrent",
        session_id="session-w06",
        text="只应执行一次",
        created_at=NOW,
    )
    fingerprint = message_fingerprint(message)

    async def scenario() -> None:
        claims = await asyncio.gather(
            *(
                store.claim(
                    _key(message.message_id),
                    fingerprint=fingerprint,
                    state=_state(message.message_id, turn_id=f"turn-{index}"),
                    now=NOW,
                )
                for index in range(32)
            )
        )

        assert sum(claim.created for claim in claims) == 1
        assert len({claim.state.turn_id for claim in claims}) == 1
        assert await store.count_records(client_id="client-w06", session_id="session-w06") == 1

    asyncio.run(scenario())


def test_same_key_changed_body_and_cross_client_session_owner_are_rejected(
    tmp_path: Path,
) -> None:
    _database, store = _store(tmp_path)
    first = UserMessage(
        message_id="message-conflict",
        session_id="session-w06",
        text="原始内容",
        created_at=NOW,
    )
    changed = first.model_copy(
        update={"text": "被替换的内容", "created_at": NOW + timedelta(seconds=1)}
    )

    async def scenario() -> None:
        await store.claim(
            _key(first.message_id),
            fingerprint=message_fingerprint(first),
            state=_state(first.message_id, turn_id="turn-original"),
            now=NOW,
        )
        with pytest.raises(IdempotencyConflictError):
            await store.claim(
                _key(changed.message_id),
                fingerprint=message_fingerprint(changed),
                state=_state(changed.message_id, turn_id="turn-changed"),
                now=NOW,
            )
        with pytest.raises(IdempotencyAccessError):
            await store.bind_session("client-other", "session-w06", now=NOW)

    asyncio.run(scenario())


def test_terminal_records_use_200_and_24_hour_hard_retention(tmp_path: Path) -> None:
    _database, store = _store(tmp_path)

    async def scenario() -> None:
        for index in range(201):
            message_id = f"message-{index:03d}"
            state = _state(
                message_id,
                turn_id=f"turn-{index:03d}",
                now=NOW + timedelta(seconds=index),
            )
            await store.claim(
                _key(message_id),
                fingerprint=f"{index:064x}",
                state=state,
                now=state.created_at,
            )
            await store.update(
                client_id="client-w06",
                state=state.model_copy(
                    update={"status": TurnStatus.completed, "updated_at": state.created_at}
                ),
                now=state.created_at,
            )

        assert await store.count_records(client_id="client-w06", session_id="session-w06") == 200
        assert (
            await store.lookup_turn(
                client_id="client-w06",
                session_id="session-w06",
                turn_id="turn-000",
                now=NOW + timedelta(seconds=201),
            )
            is None
        )

        await store.bind_session(
            "client-w06",
            "session-w06",
            now=NOW + timedelta(hours=24, seconds=202),
        )
        assert await store.count_records(client_id="client-w06", session_id="session-w06") == 0

    asyncio.run(scenario())


def test_terminal_capacity_uses_lru_access_without_extending_the_24_hour_ttl(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "lru.sqlite3")
    database.initialize()
    store = SQLiteIdempotencyStore(database, terminal_limit=2)

    async def complete(message_id: str, index: int) -> TurnState:
        state = _state(
            message_id,
            turn_id=f"turn-{message_id}",
            now=NOW + timedelta(seconds=index),
        )
        await store.claim(
            _key(message_id),
            fingerprint=f"{index:064x}",
            state=state,
            now=state.created_at,
        )
        terminal = state.model_copy(
            update={"status": TurnStatus.completed, "updated_at": state.created_at}
        )
        await store.update(client_id="client-w06", state=terminal, now=state.created_at)
        return terminal

    async def scenario() -> None:
        first = await complete("lru-a", 1)
        second = await complete("lru-b", 2)

        touched = await store.claim(
            _key("lru-a"),
            fingerprint=f"{1:064x}",
            state=_state("lru-a", turn_id="unused-duplicate", now=NOW),
            now=NOW + timedelta(seconds=3),
        )
        assert not touched.created and touched.state.turn_id == first.turn_id
        await complete("lru-c", 4)

        assert (
            await store.lookup_turn(
                client_id="client-w06",
                session_id="session-w06",
                turn_id=first.turn_id,
                now=NOW + timedelta(seconds=5),
            )
            is not None
        )
        assert (
            await store.lookup_turn(
                client_id="client-w06",
                session_id="session-w06",
                turn_id=second.turn_id,
                now=NOW + timedelta(seconds=5),
            )
            is None
        )

        await store.bind_session(
            "client-w06",
            "session-w06",
            now=NOW + timedelta(hours=24, seconds=5),
        )
        assert await store.count_records(client_id="client-w06", session_id="session-w06") == 0

    asyncio.run(scenario())


def test_restart_marks_nonterminal_failed_without_rerunning_and_terminal_stays_terminal(
    tmp_path: Path,
) -> None:
    _database, store = _store(tmp_path)

    async def scenario() -> None:
        accepted = _state("accepted", turn_id="turn-accepted")
        completed = _state("completed", turn_id="turn-completed").model_copy(
            update={"status": TurnStatus.completed}
        )
        for state in (accepted, completed):
            await store.claim(
                _key(state.source_message_id),
                fingerprint=("a" if state is accepted else "b") * 64,
                state=state.model_copy(update={"status": TurnStatus.accepted}),
                now=NOW,
            )
            if state.status is TurnStatus.completed:
                await store.update(client_id="client-w06", state=state, now=NOW)

        assert await store.recover_incomplete(now=NOW + timedelta(minutes=1)) == 1
        recovered = await store.lookup_turn(
            client_id="client-w06",
            session_id="session-w06",
            turn_id="turn-accepted",
            now=NOW + timedelta(minutes=1),
        )
        terminal = await store.lookup_turn(
            client_id="client-w06",
            session_id="session-w06",
            turn_id="turn-completed",
            now=NOW + timedelta(minutes=1),
        )
        assert recovered is not None
        assert recovered.status is TurnStatus.failed
        assert recovered.error_code == "service_restarted"
        assert terminal is not None and terminal.status is TurnStatus.completed

    asyncio.run(scenario())


def test_idempotency_database_contains_fingerprint_but_no_message_body_copy(tmp_path: Path) -> None:
    database, store = _store(tmp_path)
    private = "W06-private-body-sentinel@example.invalid"
    message = UserMessage(
        message_id="message-private-scan",
        session_id="session-w06",
        text=private,
        created_at=NOW,
    )

    asyncio.run(
        store.claim(
            _key(message.message_id),
            fingerprint=message_fingerprint(message),
            state=_state(message.message_id, turn_id="turn-private-scan"),
            now=NOW,
        )
    )
    database.secure_cleanup()

    assert private.encode("utf-8") not in database.path.read_bytes()
    with database.connect() as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(idempotency_turns)")
        }
    assert "message_text" not in columns
    assert "content" not in columns
