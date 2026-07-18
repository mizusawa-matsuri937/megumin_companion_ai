"""Globally arbitrated, idempotent, bounded dialogue lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol, cast

from app.config.logging import log_event
from app.core.cancellation import CancellationToken
from app.core.contracts import TurnEventSink, TurnPriorityController
from app.core.idempotency import (
    DEFAULT_TERMINAL_LIMIT,
    DEFAULT_TERMINAL_TTL,
    TERMINAL_STATUSES,
    IdempotencyAccessError,
    IdempotencyConflictError,
    IdempotencyKey,
    IdempotencyStore,
    IdempotencyUnavailableError,
    InMemoryIdempotencyStore,
    RetainedTurn,
    message_fingerprint,
)
from app.schemas import (
    InputMode,
    PipelineEvent,
    ProactiveIntent,
    SessionReset,
    SessionSnapshot,
    SessionSnapshotChunk,
    TurnOutcome,
    TurnState,
    TurnStatus,
    UserMessage,
    utc_now,
)

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]
SubscriptionItem = PipelineEvent | SessionReset | SessionSnapshotChunk
SessionKey = tuple[str, str]
DEFAULT_CLIENT_ID = "local_client"
DEFAULT_REPLAY_LIMIT = 2_000
DEFAULT_REPLAY_TTL = timedelta(minutes=10)
DEFAULT_SUBSCRIBER_QUEUE_LIMIT = 512
SNAPSHOT_CHUNK_SIZE = 50


class TurnPipeline(Protocol):
    async def run(
        self,
        message: UserMessage,
        state: TurnState,
        token: CancellationToken,
        emit: EventEmitter,
    ) -> TurnOutcome: ...

    async def close(self) -> None: ...


class ProactiveTurnPipeline(Protocol):
    async def run_proactive(
        self,
        intent: ProactiveIntent,
        state: TurnState,
        token: CancellationToken,
        emit: EventEmitter,
    ) -> TurnOutcome: ...


class TurnObserver(Protocol):
    async def on_user_accepted(self, message: UserMessage, state: TurnState) -> None: ...

    async def on_turn_completed(
        self,
        message: UserMessage,
        state: TurnState,
        outcome: TurnOutcome,
    ) -> None: ...


class TurnAccessError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("turn_forbidden")


class SubscriptionClosedError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SlowConsumerError(SubscriptionClosedError):
    def __init__(self) -> None:
        super().__init__("slow_consumer")


class EventSubscription:
    """Bounded live queue plus a cursor over the already-bounded replay log."""

    def __init__(
        self,
        client_id: str,
        session_id: str,
        *,
        initial: Sequence[SubscriptionItem] = (),
        maxsize: int = DEFAULT_SUBSCRIBER_QUEUE_LIMIT,
    ) -> None:
        if maxsize < 1:
            raise ValueError("subscriber queue size must be positive")
        self.client_id = client_id
        self.session_id = session_id
        self.maxsize = maxsize
        self._initial: deque[SubscriptionItem] = deque(initial)
        self._live: deque[PipelineEvent] = deque()
        self._available = asyncio.Event()
        self._closed = asyncio.Event()
        self._unfinished_tasks = 0
        self.closed_reason: str | None = None
        if self._initial:
            self._available.set()

    async def get(self) -> SubscriptionItem:
        while True:
            if self.closed_reason is not None:
                if self.closed_reason == "slow_consumer":
                    raise SlowConsumerError
                raise SubscriptionClosedError(self.closed_reason)
            if self._initial:
                item = self._initial.popleft()
                self._unfinished_tasks += 1
                self._refresh_available()
                return item
            if self._live:
                item = self._live.popleft()
                self._unfinished_tasks += 1
                self._refresh_available()
                return item
            self._available.clear()
            await self._available.wait()

    def task_done(self) -> None:
        if self._unfinished_tasks <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished_tasks -= 1

    def qsize(self) -> int:
        return len(self._live)

    def replay_size(self) -> int:
        return len(self._initial)

    def offer(self, event: PipelineEvent) -> bool:
        if self.closed_reason is not None:
            return False
        if self._live and event.type == "assistant.delta":
            merged = _merge_delta(self._live[-1], event)
            if merged is not None:
                self._live[-1] = merged
                self._available.set()
                return True
        if len(self._live) >= self.maxsize:
            self.close("slow_consumer")
            return False
        self._live.append(event)
        self._available.set()
        return True

    def close(self, reason: str = "unsubscribed") -> None:
        if self.closed_reason is None:
            self.closed_reason = reason
            self._closed.set()
            self._available.set()

    async def wait_closed(self) -> str:
        await self._closed.wait()
        assert self.closed_reason is not None
        return self.closed_reason

    def _refresh_available(self) -> None:
        if not self._initial and not self._live and self.closed_reason is None:
            self._available.clear()


@dataclass(slots=True)
class _ActiveTurn:
    turn_id: str
    client_id: str
    session_id: str
    origin: str
    token: CancellationToken | None = None
    task: asyncio.Task[Any] | None = None
    cancellation_started: bool = False


@dataclass(frozen=True, slots=True)
class _OutcomeSummary:
    text_length: int
    segment_count: int
    metrics: dict[str, Any]
    origin: str | None = None


class TurnService:
    """Linearize commands, claim messages atomically, and bound client-visible state."""

    def __init__(
        self,
        logger: logging.Logger,
        pipeline: TurnPipeline | None = None,
        *,
        observers: Sequence[TurnObserver] = (),
        event_sinks: Sequence[TurnEventSink] = (),
        priority_controller: TurnPriorityController | None = None,
        idempotency_store: IdempotencyStore | None = None,
        clock: Callable[[], datetime] = utc_now,
        replay_limit: int = DEFAULT_REPLAY_LIMIT,
        replay_ttl: timedelta = DEFAULT_REPLAY_TTL,
        terminal_limit: int = DEFAULT_TERMINAL_LIMIT,
        terminal_ttl: timedelta = DEFAULT_TERMINAL_TTL,
        subscriber_queue_limit: int = DEFAULT_SUBSCRIBER_QUEUE_LIMIT,
    ) -> None:
        if (
            not 1 <= replay_limit <= DEFAULT_REPLAY_LIMIT
            or not 1 <= terminal_limit <= DEFAULT_TERMINAL_LIMIT
            or not 1 <= subscriber_queue_limit <= DEFAULT_SUBSCRIBER_QUEUE_LIMIT
            or not timedelta(0) < replay_ttl <= DEFAULT_REPLAY_TTL
            or not timedelta(0) < terminal_ttl <= DEFAULT_TERMINAL_TTL
        ):
            raise ValueError("turn service resource bounds must not exceed hard limits")
        self._logger = logger
        self._pipeline = pipeline
        self._observers = tuple(observers)
        self._event_sinks = tuple(event_sinks)
        self._priority_controller = priority_controller
        self._idempotency_store = idempotency_store or InMemoryIdempotencyStore(
            terminal_limit=terminal_limit,
            terminal_ttl=terminal_ttl,
            clock=clock,
        )
        self._clock = clock
        self._replay_limit = replay_limit
        self._replay_ttl = replay_ttl
        self._terminal_limit = terminal_limit
        self._terminal_ttl = terminal_ttl
        self._subscriber_queue_limit = subscriber_queue_limit
        self._states: dict[str, TurnState] = {}
        self._outcomes: dict[str, _OutcomeSummary] = {}
        self._turn_clients: dict[str, str] = {}
        self._idempotent_turns: set[str] = set()
        self._terminal_order: dict[str, int] = {}
        self._terminal_sequence = 0
        self._session_turn: dict[SessionKey, str] = {}
        self._session_latest: dict[SessionKey, str] = {}
        self._active: _ActiveTurn | None = None
        self._pipeline_tasks: set[asyncio.Task[Any]] = set()
        self._post_commit_tasks: set[asyncio.Task[Any]] = set()
        self._accept_depths: dict[asyncio.Task[Any], int] = {}
        self._subscribers: dict[SessionKey, set[EventSubscription]] = {}
        self._replay: dict[SessionKey, deque[PipelineEvent]] = {}
        self._session_seq: dict[SessionKey, int] = {}
        self._closed = False
        self._command_lock = asyncio.Lock()
        self._coordination_lock = asyncio.Lock()
        self._output_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_started = asyncio.Event()

    async def accept(
        self,
        message: UserMessage,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
    ) -> TurnState:
        """Atomically claim an explicit message before any observer or pipeline side effect."""

        owner = asyncio.current_task()
        assert owner is not None
        self._track_accept(owner)
        now = self._now()
        state = TurnState(
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
            created_at=now,
            updated_at=now,
        )
        key = IdempotencyKey(client_id, message.session_id, message.message_id)
        priority_owned = False
        claimed = False
        registered = False
        pipeline_started = False
        try:
            async with self._command_lock:
                if self._closed:
                    raise RuntimeError("TurnService 已关闭")
                self._prune_terminal(now)
                claim = await self._idempotency_store.claim(
                    key,
                    fingerprint=message_fingerprint(message),
                    state=state,
                    now=now,
                )
                if not claim.created:
                    existing = self._prefer_memory_state(claim.state)
                    if existing != claim.state:
                        await self._idempotency_store.update(
                            client_id=client_id,
                            state=existing,
                            now=self._now(),
                        )
                    self._remember_existing(client_id, existing)
                    await self._publish_state(
                        existing,
                        "turn.snapshot",
                        client_id=client_id,
                        notify_sinks=False,
                    )
                    return existing
                claimed = True
                self._idempotent_turns.add(state.turn_id)
                self._turn_clients[state.turn_id] = client_id

                if self._closed:
                    raise RuntimeError("TurnService 已关闭")
                priority_owned = True
                await self._begin_user_priority(state.turn_id)
                old_task, immediate = await self._signal_active_cancellation()
                if old_task is not None:
                    await self._join_task(old_task)
                if immediate is not None:
                    cancelled, event_type = immediate
                    await self._commit_terminal(cancelled, event_type)

                async with self._coordination_lock:
                    if self._closed:
                        raise RuntimeError("TurnService 已关闭")
                    self._states[state.turn_id] = state
                    session_key = (client_id, state.session_id)
                    self._session_turn[session_key] = state.turn_id
                    self._session_latest[session_key] = state.turn_id
                    self._active = _ActiveTurn(
                        turn_id=state.turn_id,
                        client_id=client_id,
                        session_id=state.session_id,
                        origin="user",
                    )
                    registered = True
                    log_event(
                        self._logger,
                        logging.INFO,
                        "turn.accepted",
                        turn_id=state.turn_id,
                        message_id=message.message_id,
                        session_id=message.session_id,
                        input_mode=message.input_mode.value,
                        text_length=len(message.text),
                    )
                    await self._publish_state(
                        state,
                        "turn.accepted",
                        client_id=client_id,
                    )

            await self._notify_user_accepted(message, state)

            if self._pipeline is not None:
                async with self._coordination_lock:
                    current = self._states[state.turn_id]
                    active = self._active
                    if (
                        not self._closed
                        and current.status is TurnStatus.accepted
                        and active is not None
                        and active.turn_id == state.turn_id
                    ):
                        token = CancellationToken(state.turn_id)
                        started = asyncio.Event()
                        task = asyncio.create_task(
                            self._run_user(message, state, token, started, client_id=client_id),
                            name=f"dialogue-{state.turn_id}",
                        )
                        active.token = token
                        active.task = task
                        self._track_pipeline_task(task)
                        await started.wait()
                        priority_owned = False
                        pipeline_started = True
            if not pipeline_started and self._pipeline is not None:
                async with self._coordination_lock:
                    self._clear_active_locked(state.turn_id)

            return state if pipeline_started else self._states[state.turn_id]
        except BaseException as exc:
            if registered:
                cancel_task, immediate = await self._cancel_registered_turn(state.turn_id)
                if cancel_task is not None:
                    await self._join_task(cancel_task)
                if immediate is not None:
                    cancelled, event_type = immediate
                    await self._safe_commit_terminal(cancelled, event_type)
            elif claimed:
                status = (
                    TurnStatus.cancelled
                    if isinstance(exc, asyncio.CancelledError)
                    else TurnStatus.failed
                )
                error_code = None if status is TurnStatus.cancelled else "accept_failed"
                failed = state.model_copy(
                    update={
                        "status": status,
                        "updated_at": self._now(),
                        "error_code": error_code,
                    }
                )
                await self._safe_persist_state(client_id, failed)
            raise
        finally:
            self._untrack_accept(owner)
            if priority_owned:
                await self._safe_end_user_priority(state.turn_id)

    async def _run_user(
        self,
        message: UserMessage,
        state: TurnState,
        token: CancellationToken,
        started: asyncio.Event,
        *,
        client_id: str,
    ) -> None:
        assert self._pipeline is not None

        async def emit(event_type: str, payload: dict[str, Any]) -> None:
            token.raise_if_cancelled()
            await self._publish(
                event_type,
                turn_id=state.turn_id,
                client_id=client_id,
                session_id=state.session_id,
                payload=payload,
            )

        try:
            running = self._set_status(state.turn_id, TurnStatus.streaming)
            await self._idempotency_store.update(
                client_id=client_id,
                state=running,
                now=self._now(),
            )
            started.set()
            async with self._output_lock:
                token.raise_if_cancelled()
                outcome = await self._pipeline.run(message, state, token, emit)
            token.raise_if_cancelled()
            self._outcomes[state.turn_id] = _outcome_summary(outcome)
            completed = self._set_status(state.turn_id, TurnStatus.completed)
            self._clear_active_locked(state.turn_id)
            await self._safe_persist_state(client_id, completed)
            self._prune_terminal(self._now())
            finalizer = asyncio.create_task(
                self._finalize_completed(message, completed, outcome, client_id=client_id),
                name=f"turn-post-commit-{state.turn_id}",
            )
            self._track_post_commit_task(finalizer)
        except asyncio.CancelledError:
            cancelled, changed = self._set_terminal_if_open(state.turn_id, TurnStatus.cancelled)
            self._clear_active_locked(state.turn_id)
            if changed:
                await self._safe_persist_state(client_id, cancelled)
                self._prune_terminal(self._now())
                await self._publish_state(
                    cancelled,
                    "turn.cancelled",
                    client_id=client_id,
                )
                log_event(
                    self._logger,
                    logging.INFO,
                    "turn.cancelled",
                    turn_id=state.turn_id,
                    session_id=state.session_id,
                )
            raise
        except Exception as exc:
            failed, changed = self._set_terminal_if_open(
                state.turn_id,
                TurnStatus.failed,
                error_code=_safe_error_code(exc),
            )
            self._clear_active_locked(state.turn_id)
            if changed:
                await self._safe_persist_state(client_id, failed)
                self._prune_terminal(self._now())
                await self._publish_state(failed, "turn.failed", client_id=client_id)
                log_event(
                    self._logger,
                    logging.ERROR,
                    "turn.failed",
                    turn_id=state.turn_id,
                    session_id=state.session_id,
                    error_code=failed.error_code,
                )
        finally:
            started.set()
            await self._safe_end_user_priority(state.turn_id)

    async def _finalize_completed(
        self,
        message: UserMessage,
        completed: TurnState,
        outcome: TurnOutcome,
        *,
        client_id: str,
    ) -> None:
        await self._notify_turn_completed(message, completed, outcome)
        await self._publish(
            "assistant.completed",
            turn_id=completed.turn_id,
            client_id=client_id,
            session_id=completed.session_id,
            payload={
                "state": completed.model_dump(mode="json"),
                "metrics": outcome.metrics.model_dump(mode="json"),
            },
        )
        log_event(
            self._logger,
            logging.INFO,
            "turn.completed",
            turn_id=completed.turn_id,
            session_id=completed.session_id,
            **outcome.metrics.model_dump(mode="json"),
        )

    async def run_proactive(
        self,
        intent: ProactiveIntent,
        token: CancellationToken,
    ) -> None:
        """Run an internal intent without creating a user-message idempotency record."""

        if self._pipeline is None:
            return
        task = asyncio.current_task()
        assert task is not None
        now = self._now()
        state = TurnState(
            session_id="local_session",
            source_message_id=intent.intent_id,
            input_mode=InputMode.text,
            created_at=now,
            updated_at=now,
        )

        async def emit(event_type: str, payload: dict[str, Any]) -> None:
            token.raise_if_cancelled()
            await self._publish(
                event_type,
                turn_id=state.turn_id,
                client_id=DEFAULT_CLIENT_ID,
                session_id=state.session_id,
                payload=payload,
            )

        async with self._coordination_lock:
            if self._closed or self._active is not None:
                token.cancel()
                token.raise_if_cancelled()
            self._states[state.turn_id] = state
            self._turn_clients[state.turn_id] = DEFAULT_CLIENT_ID
            self._session_turn[(DEFAULT_CLIENT_ID, state.session_id)] = state.turn_id
            self._session_latest[(DEFAULT_CLIENT_ID, state.session_id)] = state.turn_id
            self._active = _ActiveTurn(
                turn_id=state.turn_id,
                client_id=DEFAULT_CLIENT_ID,
                session_id=state.session_id,
                origin="proactive",
                token=token,
                task=task,
            )
            self._track_pipeline_task(task)
            await self._publish(
                "proactive.accepted",
                turn_id=state.turn_id,
                client_id=DEFAULT_CLIENT_ID,
                session_id=state.session_id,
                payload={
                    "intent_id": intent.intent_id,
                    "trigger_type": intent.trigger_type,
                    "voice_allowed": intent.voice_allowed,
                },
            )
        try:
            self._set_status(state.turn_id, TurnStatus.streaming)
            async with self._output_lock:
                token.raise_if_cancelled()
                proactive_pipeline = cast(ProactiveTurnPipeline, self._pipeline)
                outcome = await proactive_pipeline.run_proactive(intent, state, token, emit)
            token.raise_if_cancelled()
            outcome = outcome.model_copy(
                update={
                    "metadata": {
                        **outcome.metadata,
                        "origin": "proactive",
                        "intent_id": intent.intent_id,
                    }
                }
            )
            self._outcomes[state.turn_id] = _outcome_summary(outcome)
            completed = self._set_status(state.turn_id, TurnStatus.completed)
            self._clear_active_locked(state.turn_id)
            self._prune_terminal(self._now())
            await self._publish_state(
                completed,
                "proactive.completed",
                client_id=DEFAULT_CLIENT_ID,
            )
        except asyncio.CancelledError:
            cancelled, changed = self._set_terminal_if_open(state.turn_id, TurnStatus.cancelled)
            self._clear_active_locked(state.turn_id)
            if changed:
                self._prune_terminal(self._now())
                await self._publish_state(
                    cancelled,
                    "proactive.cancelled",
                    client_id=DEFAULT_CLIENT_ID,
                )
            raise
        except Exception as exc:
            failed, changed = self._set_terminal_if_open(
                state.turn_id,
                TurnStatus.failed,
                error_code=_safe_error_code(exc),
            )
            self._clear_active_locked(state.turn_id)
            if changed:
                self._prune_terminal(self._now())
                await self._publish_state(
                    failed,
                    "proactive.failed",
                    client_id=DEFAULT_CLIENT_ID,
                )
        finally:
            self._clear_active_locked(state.turn_id)

    async def cancel(
        self,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        session_id: str = "local_session",
        turn_id: str | None = None,
        reason: str = "user_interrupt",
    ) -> TurnState | None:
        async with self._command_lock:
            now = self._now()
            await self._hydrate_session(client_id, session_id, now=now)
            session_key = (client_id, session_id)
            target_id = (
                turn_id
                or self._session_turn.get(session_key)
                or self._session_latest.get(session_key)
            )
            if target_id is None:
                return None
            state = self._states.get(target_id)
            if state is None:
                state = await self._idempotency_store.lookup_turn(
                    client_id=client_id,
                    session_id=session_id,
                    turn_id=target_id,
                    now=self._now(),
                )
                if state is None:
                    return None
                self._remember_existing(client_id, state)
            if (
                state.session_id != session_id
                or self._turn_clients.get(target_id, client_id) != client_id
            ):
                raise TurnAccessError
            if state.status in TERMINAL_STATUSES:
                if state.turn_id in self._idempotent_turns:
                    stored = await self._idempotency_store.lookup_turn(
                        client_id=client_id,
                        session_id=session_id,
                        turn_id=target_id,
                        now=self._now(),
                    )
                    if stored is None:
                        self._evict_turn(target_id)
                        return None
                    state = self._prefer_memory_state(stored)
                    if state != stored:
                        await self._idempotency_store.update(
                            client_id=client_id,
                            state=state,
                            now=self._now(),
                        )
                    self._states[target_id] = state
                self._touch_terminal(state.turn_id)
                self._prune_terminal(self._now())
            task, immediate = await self._cancel_registered_turn(target_id)
            if immediate is not None:
                cancelled, event_type = immediate
                await self._commit_terminal(cancelled, event_type)
            if task is not None:
                await self._join_task(task)
            result = self._states.get(target_id, state)
            log_event(
                self._logger,
                logging.INFO,
                "turn.cancel.requested",
                turn_id=target_id,
                session_id=result.session_id,
                reason=reason,
            )
            return result

    async def _signal_active_cancellation(
        self,
    ) -> tuple[asyncio.Task[Any] | None, tuple[TurnState, str] | None]:
        async with self._coordination_lock:
            return self._signal_active_cancellation_locked()

    async def _cancel_registered_turn(
        self, turn_id: str
    ) -> tuple[asyncio.Task[Any] | None, tuple[TurnState, str] | None]:
        async with self._coordination_lock:
            state = self._states.get(turn_id)
            if state is None or state.status in TERMINAL_STATUSES:
                return None, None
            active = self._active
            if active is not None and active.turn_id == turn_id:
                return self._signal_active_cancellation_locked()
            cancelled = self._set_status(turn_id, TurnStatus.cancelled)
            self._clear_active_locked(turn_id)
            return None, (cancelled, "turn.cancelled")

    def _signal_active_cancellation_locked(
        self,
    ) -> tuple[asyncio.Task[Any] | None, tuple[TurnState, str] | None]:
        active = self._active
        if active is None:
            return None, None
        state = self._states[active.turn_id]
        event_type = "proactive.cancelled" if active.origin == "proactive" else "turn.cancelled"
        if state.status in TERMINAL_STATUSES:
            self._clear_active_locked(active.turn_id)
            return None, None
        task = active.task
        if task is None or task.done():
            cancelled = self._set_status(active.turn_id, TurnStatus.cancelled)
            self._clear_active_locked(active.turn_id)
            return None, (cancelled, event_type)
        if not active.cancellation_started:
            active.cancellation_started = True
            token = active.token
            already_signalled = token is not None and token.cancelled
            if token is not None:
                token.cancel()
            if not already_signalled and task is not asyncio.current_task():
                task.cancel()
        return task, None

    async def subscribe(
        self,
        session_id: str,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        last_seq: int = 0,
    ) -> EventSubscription:
        if session_id == "*" or last_seq < 0:
            raise IdempotencyAccessError
        async with self._command_lock:
            now = self._now()
            await self._idempotency_store.bind_session(client_id, session_id, now=now)
            retained = await self._hydrate_session(client_id, session_id, now=now)
            key = (client_id, session_id)
            self._prune_terminal(now)
            self._prune_replay(key, now)
            latest = self._session_seq.get(key, 0)
            replay = self._replay.get(key, deque())
            initial: tuple[SubscriptionItem, ...]
            if last_seq == latest and not (latest == 0 and retained):
                initial = ()
            elif replay and replay[0].seq - 1 <= last_seq < latest:
                initial = tuple(event for event in replay if event.seq > last_seq)
            else:
                snapshot, states = self._session_snapshot(client_id, session_id)
                reset = SessionReset(
                    session_id=session_id,
                    requested_last_seq=last_seq,
                    reset_to_seq=latest,
                    snapshot=snapshot,
                )
                chunks = tuple(
                    SessionSnapshotChunk(
                        reset_id=reset.reset_id,
                        session_id=session_id,
                        chunk_index=index,
                        chunk_count=snapshot.chunk_count,
                        turns=list(states[offset : offset + SNAPSHOT_CHUNK_SIZE]),
                    )
                    for index, offset in enumerate(range(0, len(states), SNAPSHOT_CHUNK_SIZE))
                )
                initial = (reset, *chunks)
            subscription = EventSubscription(
                client_id,
                session_id,
                initial=initial,
                maxsize=self._subscriber_queue_limit,
            )
            self._subscribers.setdefault(key, set()).add(subscription)
            return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        key = (subscription.client_id, subscription.session_id)
        subscribers = self._subscribers.get(key)
        if subscribers is not None:
            subscribers.discard(subscription)
            if not subscribers:
                self._subscribers.pop(key, None)
        subscription.close()

    async def _publish(
        self,
        event_type: str,
        *,
        turn_id: str | None,
        client_id: str,
        session_id: str,
        payload: dict[str, Any],
        notify_sinks: bool = True,
    ) -> PipelineEvent:
        key = (client_id, session_id)
        now = self._now()
        seq = self._session_seq.get(key, 0) + 1
        self._session_seq[key] = seq
        event = PipelineEvent(
            seq=seq,
            type=event_type,
            turn_id=turn_id,
            session_id=session_id,
            payload=payload,
            emitted_at=now,
        )
        replay = self._replay.setdefault(key, deque())
        replay.append(event)
        self._prune_replay(key, now)
        if notify_sinks:
            for sink in self._event_sinks:
                try:
                    accepted = sink.publish(event)
                    if not accepted:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "turn.event_sink_dropped",
                            sink=type(sink).__name__,
                            event_type=event.type,
                            turn_id=event.turn_id,
                        )
                except Exception:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "turn.event_sink_failed",
                        sink=type(sink).__name__,
                        event_type=event.type,
                        turn_id=event.turn_id,
                    )
        for subscription in tuple(self._subscribers.get(key, ())):
            subscription.offer(event)
        return event

    async def _publish_state(
        self,
        state: TurnState,
        event_type: str,
        *,
        client_id: str,
        notify_sinks: bool = True,
    ) -> PipelineEvent:
        return await self._publish(
            event_type,
            turn_id=state.turn_id,
            client_id=client_id,
            session_id=state.session_id,
            payload=state.model_dump(mode="json"),
            notify_sinks=notify_sinks,
        )

    def _set_status(
        self,
        turn_id: str,
        status: TurnStatus,
        *,
        error_code: str | None = None,
    ) -> TurnState:
        state = self._states[turn_id].model_copy(
            update={"status": status, "updated_at": self._now(), "error_code": error_code}
        )
        self._states[turn_id] = state
        return state

    def _set_terminal_if_open(
        self,
        turn_id: str,
        status: TurnStatus,
        *,
        error_code: str | None = None,
    ) -> tuple[TurnState, bool]:
        state = self._states[turn_id]
        if state.status in TERMINAL_STATUSES:
            return state, False
        return self._set_status(turn_id, status, error_code=error_code), True

    def _clear_active_locked(self, turn_id: str) -> None:
        active = self._active
        if active is not None and active.turn_id == turn_id:
            self._active = None
        state = self._states.get(turn_id)
        client_id = self._turn_clients.get(turn_id)
        if state is not None and client_id is not None:
            key = (client_id, state.session_id)
            if self._session_turn.get(key) == turn_id:
                self._session_turn.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        now = self._now()
        self._prune_terminal(now)
        for key in tuple(self._replay):
            self._prune_replay(key, now)
        active = self._active
        return {
            "active_turns": [active.turn_id] if active is not None else [],
            "turns": {
                turn_id: state.model_dump(mode="json") for turn_id, state in self._states.items()
            },
            "metrics": {turn_id: summary.metrics for turn_id, summary in self._outcomes.items()},
            "outcomes": {
                turn_id: {
                    "text_length": summary.text_length,
                    "segment_count": summary.segment_count,
                    **({"origin": summary.origin} if summary.origin is not None else {}),
                }
                for turn_id, summary in self._outcomes.items()
            },
            "subscriber_count": sum(len(group) for group in self._subscribers.values()),
            "replay_event_count": sum(len(events) for events in self._replay.values()),
        }

    async def wait_idle(self) -> None:
        await self._drain_tasks(tuple(self._pipeline_tasks))
        await self._drain_tasks(tuple(self._post_commit_tasks))

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(
                    self._shutdown_impl(), name="turn-service-shutdown"
                )
            task = self._shutdown_task
        current = asyncio.current_task()
        if current is not None and self._is_owned_task(current):
            await self._shutdown_started.wait()
            return
        await asyncio.shield(task)

    async def _shutdown_impl(self) -> None:
        immediate: tuple[TurnState, str] | None
        async with self._coordination_lock:
            self._closed = True
            _active_task, immediate = self._signal_active_cancellation_locked()
            self._shutdown_started.set()
        if immediate is not None:
            state, event_type = immediate
            await self._safe_commit_terminal(state, event_type)

        await self._drain_tasks(tuple(self._accept_depths))
        await self._drain_tasks(tuple(self._pipeline_tasks))
        await self._drain_tasks(tuple(self._post_commit_tasks))

        for subscriptions in tuple(self._subscribers.values()):
            for subscription in tuple(subscriptions):
                subscription.close("service_shutdown")
        self._subscribers.clear()

        closers: list[tuple[str, Awaitable[None]]] = []
        if self._pipeline is not None:
            closers.append(("pipeline", self._pipeline.close()))
        closers.extend(
            (f"event_sink:{type(sink).__name__}", sink.close()) for sink in self._event_sinks
        )
        if closers:
            results = await asyncio.gather(
                *(closer for _name, closer in closers), return_exceptions=True
            )
            for (name, _closer), result in zip(closers, results, strict=True):
                if isinstance(result, BaseException):
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "turn.shutdown_resource_failed",
                        resource=name,
                    )

    async def _commit_terminal(self, state: TurnState, event_type: str) -> None:
        client_id = self._turn_clients.get(state.turn_id, DEFAULT_CLIENT_ID)
        if state.turn_id in self._idempotent_turns:
            await self._idempotency_store.update(
                client_id=client_id,
                state=state,
                now=self._now(),
            )
        self._prune_terminal(self._now())
        await self._publish_state(state, event_type, client_id=client_id)

    async def _safe_commit_terminal(self, state: TurnState, event_type: str) -> None:
        client_id = self._turn_clients.get(state.turn_id, DEFAULT_CLIENT_ID)
        await self._safe_persist_state(client_id, state)
        self._prune_terminal(self._now())
        await self._publish_state(state, event_type, client_id=client_id)

    async def _safe_persist_state(self, client_id: str, state: TurnState) -> None:
        if state.turn_id not in self._idempotent_turns:
            return
        try:
            await self._idempotency_store.update(
                client_id=client_id,
                state=state,
                now=self._now(),
            )
        except Exception:
            log_event(
                self._logger,
                logging.ERROR,
                "turn.idempotency_update_failed",
                turn_id=state.turn_id,
                status=state.status.value,
            )

    def _prefer_memory_state(self, stored: TurnState) -> TurnState:
        memory = self._states.get(stored.turn_id)
        if memory is None:
            return stored
        if stored.status in TERMINAL_STATUSES:
            if memory.status is stored.status and memory.updated_at > stored.updated_at:
                return memory
            return stored
        if memory.status in TERMINAL_STATUSES:
            return memory
        progress = {
            TurnStatus.accepted: 0,
            TurnStatus.streaming: 1,
            TurnStatus.speaking: 2,
        }
        memory_progress = progress[memory.status]
        stored_progress = progress[stored.status]
        if memory_progress != stored_progress:
            return memory if memory_progress > stored_progress else stored
        return memory if memory.updated_at > stored.updated_at else stored

    def _remember_existing(self, client_id: str, state: TurnState) -> None:
        self._states[state.turn_id] = state
        self._turn_clients[state.turn_id] = client_id
        self._idempotent_turns.add(state.turn_id)
        self._session_latest[(client_id, state.session_id)] = state.turn_id
        if state.status in TERMINAL_STATUSES:
            self._touch_terminal(state.turn_id)
        self._prune_terminal(self._now())

    def _touch_terminal(self, turn_id: str) -> None:
        self._terminal_sequence += 1
        self._terminal_order[turn_id] = self._terminal_sequence

    def _prune_terminal(self, now: datetime) -> None:
        cutoff = now.astimezone(UTC) - self._terminal_ttl
        groups: dict[SessionKey, list[TurnState]] = {}
        for state in tuple(self._states.values()):
            if state.status not in TERMINAL_STATUSES:
                continue
            if state.turn_id not in self._terminal_order:
                self._terminal_sequence += 1
                self._terminal_order[state.turn_id] = self._terminal_sequence
            client_id = self._turn_clients.get(state.turn_id, DEFAULT_CLIENT_ID)
            if state.updated_at.astimezone(UTC) < cutoff:
                self._evict_turn(state.turn_id)
                continue
            groups.setdefault((client_id, state.session_id), []).append(state)
        for states in groups.values():
            states.sort(
                key=lambda item: self._terminal_order[item.turn_id],
                reverse=True,
            )
            for state in states[self._terminal_limit :]:
                self._evict_turn(state.turn_id)

    def _evict_turn(self, turn_id: str) -> None:
        state = self._states.pop(turn_id, None)
        client_id = self._turn_clients.get(turn_id)
        if state is not None and client_id is not None:
            key = (client_id, state.session_id)
            if self._session_latest.get(key) == turn_id:
                self._session_latest.pop(key, None)
        self._outcomes.pop(turn_id, None)
        self._terminal_order.pop(turn_id, None)
        self._turn_clients.pop(turn_id, None)
        self._idempotent_turns.discard(turn_id)
        if state is not None and client_id is not None:
            key = (client_id, state.session_id)
            if key not in self._session_latest:
                retained = [
                    candidate
                    for candidate in self._states.values()
                    if self._turn_clients.get(candidate.turn_id) == client_id
                    and candidate.session_id == state.session_id
                ]
                if retained:
                    latest = max(
                        retained,
                        key=lambda candidate: self._terminal_order.get(
                            candidate.turn_id, self._terminal_sequence + 1
                        ),
                    )
                    self._session_latest[key] = latest.turn_id

    def _prune_replay(self, key: SessionKey, now: datetime) -> None:
        replay = self._replay.get(key)
        if replay is None:
            return
        cutoff = now.astimezone(UTC) - self._replay_ttl
        while replay and replay[0].emitted_at.astimezone(UTC) < cutoff:
            replay.popleft()
        while len(replay) > self._replay_limit:
            replay.popleft()
        if not replay:
            self._replay.pop(key, None)

    async def _hydrate_session(
        self,
        client_id: str,
        session_id: str,
        *,
        now: datetime,
        _repair_attempt: int = 0,
    ) -> tuple[RetainedTurn, ...]:
        retained = await self._idempotency_store.list_session(
            client_id=client_id,
            session_id=session_id,
            now=now,
        )
        retained_ids = {item.state.turn_id for item in retained}
        stale_ids = [
            turn_id
            for turn_id in self._idempotent_turns
            if self._turn_clients.get(turn_id) == client_id
            and self._states[turn_id].session_id == session_id
            and turn_id not in retained_ids
        ]
        for turn_id in stale_ids:
            self._evict_turn(turn_id)

        repaired = False
        resolved_states: list[TurnState] = []
        for item in retained:
            stored = item.state
            state = self._prefer_memory_state(stored)
            if state != stored:
                try:
                    await self._idempotency_store.update(
                        client_id=client_id,
                        state=state,
                        now=now,
                    )
                except IdempotencyConflictError:
                    repaired = True
                    continue
                repaired = True
                continue
            self._states[state.turn_id] = state
            self._turn_clients[state.turn_id] = client_id
            self._idempotent_turns.add(state.turn_id)
            if state.status in TERMINAL_STATUSES:
                if item.terminal_order is None:
                    raise IdempotencyUnavailableError
                self._terminal_order[state.turn_id] = item.terminal_order
                self._terminal_sequence = max(self._terminal_sequence, item.terminal_order)
            resolved_states.append(state)

        if repaired:
            if _repair_attempt >= 1:
                raise IdempotencyUnavailableError
            return await self._hydrate_session(
                client_id,
                session_id,
                now=now,
                _repair_attempt=_repair_attempt + 1,
            )

        key = (client_id, session_id)
        if resolved_states:
            latest = max(
                resolved_states,
                key=lambda state: (state.created_at, state.turn_id),
            )
            self._session_latest[key] = latest.turn_id
        else:
            self._session_latest.pop(key, None)
        self._prune_terminal(now)
        return retained

    def _session_snapshot(
        self,
        client_id: str,
        session_id: str,
    ) -> tuple[SessionSnapshot, tuple[TurnState, ...]]:
        key = (client_id, session_id)
        states = [
            state
            for turn_id, state in self._states.items()
            if self._turn_clients.get(turn_id) == client_id and state.session_id == session_id
        ]
        states.sort(key=lambda state: (state.created_at, state.turn_id))
        active = self._active
        active_ids = (
            [active.turn_id]
            if active is not None
            and active.client_id == client_id
            and active.session_id == session_id
            else []
        )
        state_tuple = tuple(states)
        chunk_count = (len(state_tuple) + SNAPSHOT_CHUNK_SIZE - 1) // SNAPSHOT_CHUNK_SIZE
        return (
            SessionSnapshot(
                session_id=session_id,
                last_seq=self._session_seq.get(key, 0),
                active_turn_ids=active_ids,
                turn_count=len(state_tuple),
                chunk_count=chunk_count,
            ),
            state_tuple,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("turn service clock must return a timezone-aware datetime")
        return value

    async def _drain_tasks(self, initial: Sequence[asyncio.Task[Any]]) -> None:
        tasks = tuple(task for task in initial if not task.done())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _join_task(self, task: asyncio.Task[Any]) -> None:
        if task is asyncio.current_task():
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            return

    def _track_accept(self, task: asyncio.Task[Any]) -> None:
        self._accept_depths[task] = self._accept_depths.get(task, 0) + 1

    def _untrack_accept(self, task: asyncio.Task[Any]) -> None:
        remaining = self._accept_depths[task] - 1
        if remaining:
            self._accept_depths[task] = remaining
        else:
            self._accept_depths.pop(task, None)

    def _track_pipeline_task(self, task: asyncio.Task[Any]) -> None:
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_task_done)

    def _pipeline_task_done(self, task: asyncio.Task[Any]) -> None:
        self._pipeline_tasks.discard(task)
        _consume_task_result(task)

    def _track_post_commit_task(self, task: asyncio.Task[Any]) -> None:
        self._post_commit_tasks.add(task)
        task.add_done_callback(self._post_commit_task_done)

    def _post_commit_task_done(self, task: asyncio.Task[Any]) -> None:
        self._post_commit_tasks.discard(task)
        _consume_task_result(task)

    def _is_owned_task(self, task: asyncio.Task[Any]) -> bool:
        return (
            task in self._accept_depths
            or task in self._pipeline_tasks
            or task in self._post_commit_tasks
        )

    async def _begin_user_priority(self, turn_id: str) -> None:
        if self._priority_controller is not None:
            await self._priority_controller.begin_user_turn(turn_id)

    async def _safe_end_user_priority(self, turn_id: str) -> None:
        if self._priority_controller is None:
            return
        try:
            await self._priority_controller.end_user_turn(turn_id)
        except Exception:
            log_event(
                self._logger,
                logging.ERROR,
                "turn.priority_release_failed",
                turn_id=turn_id,
            )

    async def _notify_user_accepted(self, message: UserMessage, state: TurnState) -> None:
        for observer in self._observers:
            try:
                await observer.on_user_accepted(message, state)
            except Exception:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "turn.observer_failed",
                    observer=type(observer).__name__,
                    phase="user_accepted",
                    turn_id=state.turn_id,
                )

    async def _notify_turn_completed(
        self,
        message: UserMessage,
        state: TurnState,
        outcome: TurnOutcome,
    ) -> None:
        for observer in self._observers:
            try:
                await observer.on_turn_completed(message, state, outcome)
            except Exception:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "turn.observer_failed",
                    observer=type(observer).__name__,
                    phase="turn_completed",
                    turn_id=state.turn_id,
                )


def _merge_delta(previous: PipelineEvent, current: PipelineEvent) -> PipelineEvent | None:
    if (
        previous.type != "assistant.delta"
        or previous.turn_id != current.turn_id
        or previous.session_id != current.session_id
    ):
        return None
    previous_delta = previous.payload.get("delta")
    current_delta = current.payload.get("delta")
    if not isinstance(previous_delta, str) or not isinstance(current_delta, str):
        return None
    previous_other = {
        key: value
        for key, value in previous.payload.items()
        if key
        not in {
            "delta",
            "coalesced_from_seq",
            "coalesced_through_seq",
            "coalesced_event_count",
        }
    }
    current_other = {key: value for key, value in current.payload.items() if key != "delta"}
    if previous_other != current_other:
        return None
    count = previous.payload.get("coalesced_event_count", 1)
    if not isinstance(count, int) or count < 1:
        count = 1
    payload = {
        **current_other,
        "delta": previous_delta + current_delta,
        "coalesced_from_seq": previous.payload.get("coalesced_from_seq", previous.seq),
        "coalesced_through_seq": current.seq,
        "coalesced_event_count": count + 1,
    }
    return current.model_copy(update={"payload": payload})


def _outcome_summary(outcome: TurnOutcome) -> _OutcomeSummary:
    origin = outcome.metadata.get("origin")
    return _OutcomeSummary(
        text_length=len(outcome.full_text),
        segment_count=len(outcome.segments),
        metrics=outcome.metrics.model_dump(mode="json"),
        origin=origin if isinstance(origin, str) else None,
    )


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


def _safe_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, Enum) and isinstance(code.value, str):
        code = code.value
    if isinstance(code, str) and 1 <= len(code) <= 128:
        return code
    if str(exc) == "idempotency_unavailable":
        return "idempotency_unavailable"
    return "pipeline_failed"
