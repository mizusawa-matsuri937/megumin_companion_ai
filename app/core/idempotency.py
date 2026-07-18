"""Atomic message-idempotency contracts with a bounded in-process implementation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.schemas import TurnState, TurnStatus, UserMessage, utc_now

TERMINAL_STATUSES = frozenset({TurnStatus.cancelled, TurnStatus.completed, TurnStatus.failed})
DEFAULT_TERMINAL_LIMIT = 200
DEFAULT_TERMINAL_TTL = timedelta(hours=24)


class IdempotencyError(RuntimeError):
    """Base class for stable, body-free idempotency failures."""


class IdempotencyUnavailableError(IdempotencyError):
    def __init__(self) -> None:
        super().__init__("idempotency_unavailable")


class IdempotencyConflictError(IdempotencyError):
    def __init__(self) -> None:
        super().__init__("idempotency_conflict")


class IdempotencyAccessError(IdempotencyError):
    def __init__(self) -> None:
        super().__init__("identity_forbidden")


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    client_id: str
    session_id: str
    message_id: str

    def __post_init__(self) -> None:
        if not all(1 <= len(value) <= 128 for value in self.as_tuple()):
            raise ValueError("idempotency identifiers must contain 1 to 128 characters")

    def as_tuple(self) -> tuple[str, str, str]:
        return self.client_id, self.session_id, self.message_id


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    state: TurnState
    created: bool


@dataclass(frozen=True, slots=True)
class RetainedTurn:
    """A body-free retained state plus its storage-authoritative LRU order."""

    state: TurnState
    terminal_order: int | None


class IdempotencyStore(Protocol):
    async def bind_session(self, client_id: str, session_id: str, *, now: datetime) -> None: ...

    async def claim(
        self,
        key: IdempotencyKey,
        *,
        fingerprint: str,
        state: TurnState,
        now: datetime,
    ) -> IdempotencyClaim: ...

    async def update(
        self,
        *,
        client_id: str,
        state: TurnState,
        now: datetime,
    ) -> None: ...

    async def lookup_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> TurnState | None: ...

    async def list_session(
        self,
        *,
        client_id: str,
        session_id: str,
        now: datetime,
    ) -> tuple[RetainedTurn, ...]: ...

    async def recover_incomplete(self, *, now: datetime) -> int: ...


@dataclass(slots=True)
class _MemoryRecord:
    key: IdempotencyKey
    fingerprint: str
    state: TurnState
    terminal_order: int | None = None


class InMemoryIdempotencyStore:
    """Bounded test/local store; production composition injects the SQLite adapter."""

    def __init__(
        self,
        *,
        terminal_limit: int = DEFAULT_TERMINAL_LIMIT,
        terminal_ttl: timedelta = DEFAULT_TERMINAL_TTL,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if (
            not 1 <= terminal_limit <= DEFAULT_TERMINAL_LIMIT
            or not timedelta(0) < terminal_ttl <= DEFAULT_TERMINAL_TTL
        ):
            raise ValueError("idempotency retention bounds must not exceed hard limits")
        self._terminal_limit = terminal_limit
        self._terminal_ttl = terminal_ttl
        self._clock = clock
        self._records: dict[IdempotencyKey, _MemoryRecord] = {}
        self._turn_keys: dict[str, IdempotencyKey] = {}
        self._session_clients: dict[str, str] = {}
        self._terminal_sequence = 0
        self._lock = asyncio.Lock()

    async def bind_session(self, client_id: str, session_id: str, *, now: datetime) -> None:
        _require_aware(now)
        async with self._lock:
            self._bind_session_locked(client_id, session_id)
            self._prune_locked(client_id, session_id, now=now)

    async def claim(
        self,
        key: IdempotencyKey,
        *,
        fingerprint: str,
        state: TurnState,
        now: datetime,
    ) -> IdempotencyClaim:
        validate_fingerprint(fingerprint)
        _validate_claim_shape(key, state)
        _require_aware(now)
        async with self._lock:
            self._bind_session_locked(key.client_id, key.session_id)
            self._prune_locked(key.client_id, key.session_id, now=now)
            existing = self._records.get(key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflictError
                self._touch_terminal_locked(existing)
                return IdempotencyClaim(state=existing.state, created=False)
            if state.turn_id in self._turn_keys:
                raise IdempotencyConflictError
            record = _MemoryRecord(key=key, fingerprint=fingerprint, state=state)
            self._records[key] = record
            self._turn_keys[state.turn_id] = key
            return IdempotencyClaim(state=state, created=True)

    async def update(
        self,
        *,
        client_id: str,
        state: TurnState,
        now: datetime,
    ) -> None:
        _require_aware(now)
        async with self._lock:
            key = self._turn_keys.get(state.turn_id)
            if key is None:
                raise IdempotencyConflictError
            if client_id != key.client_id or state.session_id != key.session_id:
                raise IdempotencyAccessError
            record = self._records[key]
            if state.source_message_id != key.message_id:
                raise IdempotencyConflictError
            validate_state_transition(record.state.status, state.status)
            if state.status in TERMINAL_STATUSES and record.terminal_order is None:
                self._terminal_sequence += 1
                record.terminal_order = self._terminal_sequence
            record.state = state
            self._prune_locked(client_id, state.session_id, now=now)

    async def lookup_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> TurnState | None:
        _require_aware(now)
        async with self._lock:
            owner = self._session_clients.get(session_id)
            if owner is not None and owner != client_id:
                raise IdempotencyAccessError
            self._prune_locked(client_id, session_id, now=now)
            key = self._turn_keys.get(turn_id)
            if key is None:
                return None
            if key.client_id != client_id or key.session_id != session_id:
                raise IdempotencyAccessError
            record = self._records[key]
            self._touch_terminal_locked(record)
            return record.state

    async def list_session(
        self,
        *,
        client_id: str,
        session_id: str,
        now: datetime,
    ) -> tuple[RetainedTurn, ...]:
        _require_aware(now)
        async with self._lock:
            owner = self._session_clients.get(session_id)
            if owner is not None and owner != client_id:
                raise IdempotencyAccessError
            self._prune_locked(client_id, session_id, now=now)
            retained = [
                RetainedTurn(record.state, record.terminal_order)
                for record in self._records.values()
                if record.key.client_id == client_id and record.key.session_id == session_id
            ]
            retained.sort(key=lambda item: (item.state.created_at, item.state.turn_id))
            return tuple(retained)

    async def recover_incomplete(self, *, now: datetime) -> int:
        _require_aware(now)
        recovered = 0
        async with self._lock:
            for record in self._records.values():
                if record.state.status in TERMINAL_STATUSES:
                    continue
                record.state = record.state.model_copy(
                    update={
                        "status": TurnStatus.failed,
                        "updated_at": now,
                        "error_code": "service_restarted",
                    }
                )
                self._terminal_sequence += 1
                record.terminal_order = self._terminal_sequence
                recovered += 1
            for session_id, client_id in tuple(self._session_clients.items()):
                self._prune_locked(client_id, session_id, now=now)
        return recovered

    async def count_records(self, *, client_id: str, session_id: str) -> int:
        async with self._lock:
            return sum(
                record.key.client_id == client_id and record.key.session_id == session_id
                for record in self._records.values()
            )

    def _bind_session_locked(self, client_id: str, session_id: str) -> None:
        if not 1 <= len(client_id) <= 128 or not 1 <= len(session_id) <= 128:
            raise ValueError("session identity must contain 1 to 128 characters")
        owner = self._session_clients.setdefault(session_id, client_id)
        if owner != client_id:
            raise IdempotencyAccessError

    def _prune_locked(self, client_id: str, session_id: str, *, now: datetime) -> None:
        cutoff = now.astimezone(UTC) - self._terminal_ttl
        terminal = [
            record
            for record in self._records.values()
            if record.key.client_id == client_id
            and record.key.session_id == session_id
            and record.state.status in TERMINAL_STATUSES
        ]
        expired = [
            record for record in terminal if record.state.updated_at.astimezone(UTC) < cutoff
        ]
        for record in expired:
            self._delete_locked(record)
        retained = [record for record in terminal if record not in expired]
        retained.sort(key=lambda record: record.terminal_order or 0, reverse=True)
        for record in retained[self._terminal_limit :]:
            self._delete_locked(record)

    def _delete_locked(self, record: _MemoryRecord) -> None:
        self._records.pop(record.key, None)
        self._turn_keys.pop(record.state.turn_id, None)

    def _touch_terminal_locked(self, record: _MemoryRecord) -> None:
        if record.state.status not in TERMINAL_STATUSES:
            return
        self._terminal_sequence += 1
        record.terminal_order = self._terminal_sequence


class UnavailableIdempotencyStore:
    """Explicit fail-closed adapter used when production private storage is disabled."""

    async def bind_session(self, client_id: str, session_id: str, *, now: datetime) -> None:
        raise IdempotencyUnavailableError

    async def claim(
        self,
        key: IdempotencyKey,
        *,
        fingerprint: str,
        state: TurnState,
        now: datetime,
    ) -> IdempotencyClaim:
        raise IdempotencyUnavailableError

    async def update(
        self,
        *,
        client_id: str,
        state: TurnState,
        now: datetime,
    ) -> None:
        raise IdempotencyUnavailableError

    async def lookup_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> TurnState | None:
        raise IdempotencyUnavailableError

    async def list_session(
        self,
        *,
        client_id: str,
        session_id: str,
        now: datetime,
    ) -> tuple[RetainedTurn, ...]:
        raise IdempotencyUnavailableError

    async def recover_incomplete(self, *, now: datetime) -> int:
        raise IdempotencyUnavailableError


def message_fingerprint(message: UserMessage) -> str:
    """Hash semantic input without retaining another copy of the message body."""

    payload = message.model_dump(
        mode="json",
        exclude={"created_at", "message_id", "session_id"},
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_claim_shape(key: IdempotencyKey, state: TurnState) -> None:
    if state.session_id != key.session_id or state.source_message_id != key.message_id:
        raise IdempotencyConflictError
    if state.status is not TurnStatus.accepted:
        raise IdempotencyConflictError


def validate_fingerprint(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("idempotency fingerprint must be a lowercase SHA-256 hex digest")


def validate_state_transition(previous: TurnStatus, current: TurnStatus) -> None:
    if previous in TERMINAL_STATUSES and current is not previous:
        raise IdempotencyConflictError
    allowed = {
        TurnStatus.accepted: {
            TurnStatus.accepted,
            TurnStatus.streaming,
            TurnStatus.speaking,
            *TERMINAL_STATUSES,
        },
        TurnStatus.streaming: {
            TurnStatus.streaming,
            TurnStatus.speaking,
            *TERMINAL_STATUSES,
        },
        TurnStatus.speaking: {TurnStatus.speaking, *TERMINAL_STATUSES},
        TurnStatus.completed: {TurnStatus.completed},
        TurnStatus.cancelled: {TurnStatus.cancelled},
        TurnStatus.failed: {TurnStatus.failed},
    }
    if current not in allowed[previous]:
        raise IdempotencyConflictError


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("idempotency timestamps must be timezone-aware")
