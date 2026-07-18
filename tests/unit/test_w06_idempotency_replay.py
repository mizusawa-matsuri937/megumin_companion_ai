from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.core import CancellationToken, TurnService
from app.core.idempotency import (
    IdempotencyAccessError,
    IdempotencyClaim,
    IdempotencyConflictError,
    IdempotencyKey,
    IdempotencyStore,
    IdempotencyUnavailableError,
    InMemoryIdempotencyStore,
    UnavailableIdempotencyStore,
    message_fingerprint,
)
from app.core.turns import SlowConsumerError, TurnAccessError
from app.schemas import (
    PipelineEvent,
    SessionReset,
    SessionSnapshotChunk,
    TurnMetrics,
    TurnOutcome,
    TurnState,
    TurnStatus,
    UserMessage,
)
from app.storage import SQLiteDatabase, SQLiteIdempotencyStore
from hypothesis import given, settings
from hypothesis import strategies as st


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class SideEffectPipeline:
    def __init__(self, *, wait: bool = False, delta_count: int = 1) -> None:
        self.wait = wait
        self.delta_count = delta_count
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.provider_calls = 0
        self.tts_calls = 0
        self.playback_calls = 0
        self.closed = False

    async def run(
        self,
        _message: UserMessage,
        state: TurnState,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        self.provider_calls += 1
        self.started.set()
        if self.wait:
            await self.release.wait()
        token.raise_if_cancelled()
        for _index in range(self.delta_count):
            await emit("assistant.delta", {"delta": "爆"})
        self.tts_calls += 1
        await emit("assistant.segment", {"text_length": 1})
        self.playback_calls += 1
        await emit("playback.started", {"index": 0})
        await emit("playback.finished", {"index": 0})
        return TurnOutcome(
            full_text="爆" * self.delta_count,
            segments=[],
            metrics=TurnMetrics(playback_count=1, segment_count=1),
        )

    async def close(self) -> None:
        self.closed = True


class CountingObserver:
    def __init__(self, *, gate_acceptance: bool = False) -> None:
        self.gate_acceptance = gate_acceptance
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.accepted = 0
        self.completed = 0

    async def on_user_accepted(self, _message: UserMessage, _state: TurnState) -> None:
        self.accepted += 1
        self.entered.set()
        if self.gate_acceptance:
            await self.release.wait()

    async def on_turn_completed(
        self,
        _message: UserMessage,
        _state: TurnState,
        _outcome: TurnOutcome,
    ) -> None:
        self.completed += 1


class CountingVTSSink:
    def __init__(self) -> None:
        self.action_calls = 0
        self.closed = False

    def publish(self, event: PipelineEvent) -> bool:
        if event.type == "assistant.segment":
            self.action_calls += 1
        return True

    async def close(self) -> None:
        self.closed = True


class FaultInjectingMemoryStore(InMemoryIdempotencyStore):
    def __init__(self, failures: dict[TurnStatus, int]) -> None:
        super().__init__()
        self.failures = failures

    async def update(
        self,
        *,
        client_id: str,
        state: TurnState,
        now: datetime,
    ) -> None:
        remaining = self.failures.get(state.status, 0)
        if remaining:
            if remaining > 0:
                self.failures[state.status] = remaining - 1
            raise IdempotencyUnavailableError
        await super().update(client_id=client_id, state=state, now=now)


class CompletingAfterDuplicateClaimStore(SQLiteIdempotencyStore):
    """Commit an owner terminal state after a duplicate transaction read its old state."""

    def __init__(
        self,
        database: SQLiteDatabase,
        terminal_state: TurnState,
        *,
        lookup_failure: str | None = None,
    ) -> None:
        super().__init__(database)
        self._owner_store = SQLiteIdempotencyStore(database)
        self._terminal_state = terminal_state
        self._lookup_failure = lookup_failure
        self.duplicate_claim_reads = 0

    async def claim(
        self,
        key: IdempotencyKey,
        *,
        fingerprint: str,
        state: TurnState,
        now: datetime,
    ) -> IdempotencyClaim:
        claim = await super().claim(
            key,
            fingerprint=fingerprint,
            state=state,
            now=now,
        )
        if not claim.created:
            self.duplicate_claim_reads += 1
            await self._owner_store.update(
                client_id=key.client_id,
                state=self._terminal_state,
                now=self._terminal_state.updated_at,
            )
        return claim

    async def lookup_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        turn_id: str,
        now: datetime,
    ) -> TurnState | None:
        if self._lookup_failure == "missing":
            return None
        if self._lookup_failure == "unavailable":
            raise IdempotencyUnavailableError
        if self._lookup_failure == "access":
            raise IdempotencyAccessError
        if self._lookup_failure == "conflict":
            raise IdempotencyConflictError
        return await super().lookup_turn(
            client_id=client_id,
            session_id=session_id,
            turn_id=turn_id,
            now=now,
        )


def _logger() -> logging.Logger:
    instance = logging.getLogger("test.w06")
    instance.handlers = [logging.NullHandler()]
    instance.propagate = False
    return instance


def _message(index: int = 0, *, session_id: str = "session-w06") -> UserMessage:
    return UserMessage(
        message_id=f"message-{index}",
        session_id=session_id,
        text=f"消息 {index}",
        created_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
    )


async def _receive_type(subscription: Any, event_type: str) -> PipelineEvent:
    while True:
        item = await asyncio.wait_for(subscription.get(), timeout=2)
        subscription.task_done()
        if isinstance(item, PipelineEvent) and item.type == event_type:
            return item


def test_concurrent_duplicate_has_exactly_one_provider_tts_playback_vts_and_observer() -> None:
    async def scenario() -> None:
        pipeline = SideEffectPipeline(wait=True)
        observer = CountingObserver()
        vts = CountingVTSSink()
        service = TurnService(
            _logger(),
            pipeline,
            observers=(observer,),
            event_sinks=(vts,),
        )
        subscription = await service.subscribe("session-w06", client_id="client-w06", last_seq=0)
        message = _message()

        requests = [
            asyncio.create_task(service.accept(message, client_id="client-w06"))
            for _index in range(32)
        ]
        await pipeline.started.wait()
        pipeline.release.set()
        states = await asyncio.gather(*requests)
        await _receive_type(subscription, "assistant.completed")
        await service.wait_idle()

        terminal_duplicate = await service.accept(message, client_id="client-w06")
        duplicate_snapshot = await _receive_type(subscription, "turn.snapshot")
        assert len({state.turn_id for state in states}) == 1
        assert terminal_duplicate.turn_id == states[0].turn_id == duplicate_snapshot.turn_id
        assert terminal_duplicate.status is TurnStatus.completed
        assert pipeline.provider_calls == 1
        assert pipeline.tts_calls == 1
        assert pipeline.playback_calls == 1
        assert vts.action_calls == 1
        assert observer.accepted == 1
        assert observer.completed == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_two_services_share_one_sqlite_claim_without_duplicate_side_effects(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "multi-service.sqlite3")
        database.initialize()
        pipelines = (SideEffectPipeline(), SideEffectPipeline())
        observers = (CountingObserver(), CountingObserver())
        sinks = (CountingVTSSink(), CountingVTSSink())
        services = tuple(
            TurnService(
                _logger(),
                pipelines[index],
                observers=(observers[index],),
                event_sinks=(sinks[index],),
                idempotency_store=SQLiteIdempotencyStore(database),
            )
            for index in range(2)
        )
        message = _message()

        states = await asyncio.gather(
            *(services[index % 2].accept(message, client_id="client-w06") for index in range(128))
        )
        await asyncio.gather(*(service.wait_idle() for service in services))
        terminal = await asyncio.gather(
            *(services[index % 2].accept(message, client_id="client-w06") for index in range(128))
        )

        assert len({state.turn_id for state in (*states, *terminal)}) == 1
        assert all(state.status is TurnStatus.completed for state in terminal)
        assert sum(pipeline.provider_calls for pipeline in pipelines) == 1
        assert sum(pipeline.tts_calls for pipeline in pipelines) == 1
        assert sum(pipeline.playback_calls for pipeline in pipelines) == 1
        assert sum(sink.action_calls for sink in sinks) == 1
        assert sum(observer.accepted for observer in observers) == 1
        assert sum(observer.completed for observer in observers) == 1
        await asyncio.gather(*(service.shutdown() for service in services))

    asyncio.run(scenario())


@pytest.mark.parametrize("local_status", [TurnStatus.streaming, TurnStatus.speaking])
@pytest.mark.parametrize(
    "terminal_status",
    [TurnStatus.completed, TurnStatus.cancelled, TurnStatus.failed],
)
def test_duplicate_repair_conflict_refreshes_sqlite_authoritative_terminal(
    tmp_path: Path,
    local_status: TurnStatus,
    terminal_status: TurnStatus,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(
            tmp_path / f"repair-{local_status.value}-{terminal_status.value}.sqlite3"
        )
        database.initialize()
        message = _message()
        accepted_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
        accepted = TurnState(
            turn_id="turn-repair-race",
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
            created_at=accepted_at,
            updated_at=accepted_at,
        )
        local = accepted.model_copy(
            update={
                "status": local_status,
                "updated_at": accepted_at + timedelta(seconds=1),
            }
        )
        terminal = accepted.model_copy(
            update={
                "status": terminal_status,
                "updated_at": accepted_at + timedelta(seconds=2),
                "error_code": "owner_failed" if terminal_status is TurnStatus.failed else None,
            }
        )
        owner_store = SQLiteIdempotencyStore(database)
        await owner_store.claim(
            IdempotencyKey("client-w06", message.session_id, message.message_id),
            fingerprint=message_fingerprint(message),
            state=accepted,
            now=accepted_at,
        )
        race_store = CompletingAfterDuplicateClaimStore(database, terminal)
        pipeline = SideEffectPipeline()
        service = TurnService(_logger(), pipeline, idempotency_store=race_store)
        service._remember_existing("client-w06", local)

        refreshed = await service.accept(message, client_id="client-w06")

        assert race_store.duplicate_claim_reads == 1
        assert refreshed == terminal
        assert refreshed.updated_at == terminal.updated_at
        assert service.snapshot()["turns"][accepted.turn_id]["status"] == terminal_status.value
        assert pipeline.provider_calls == pipeline.tts_calls == pipeline.playback_calls == 0
        await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("local_status", [TurnStatus.streaming, TurnStatus.speaking])
def test_duplicate_repair_preserves_valid_forward_progress_and_timestamp(
    tmp_path: Path,
    local_status: TurnStatus,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / f"repair-forward-{local_status.value}.sqlite3")
        database.initialize()
        message = _message()
        accepted_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
        accepted = TurnState(
            turn_id="turn-forward-repair",
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
            created_at=accepted_at,
            updated_at=accepted_at,
        )
        local = accepted.model_copy(
            update={
                "status": local_status,
                "updated_at": accepted_at + timedelta(seconds=1),
            }
        )
        store = SQLiteIdempotencyStore(database)
        await store.claim(
            IdempotencyKey("client-w06", message.session_id, message.message_id),
            fingerprint=message_fingerprint(message),
            state=accepted,
            now=accepted_at,
        )
        service = TurnService(_logger(), idempotency_store=store)
        service._remember_existing("client-w06", local)

        repaired = await service.accept(message, client_id="client-w06")
        stored = await store.lookup_turn(
            client_id="client-w06",
            session_id=message.session_id,
            turn_id=accepted.turn_id,
            now=accepted_at + timedelta(seconds=2),
        )

        assert repaired == local
        assert stored == local
        await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("lookup_failure", "expected_error"),
    [
        ("missing", IdempotencyUnavailableError),
        ("unavailable", IdempotencyUnavailableError),
        ("access", IdempotencyAccessError),
        ("conflict", IdempotencyConflictError),
    ],
)
def test_duplicate_repair_refresh_failure_is_fail_closed_without_pipeline(
    tmp_path: Path,
    lookup_failure: str,
    expected_error: type[Exception],
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / f"repair-failure-{lookup_failure}.sqlite3")
        database.initialize()
        message = _message()
        accepted_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
        accepted = TurnState(
            turn_id="turn-repair-failure",
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
            created_at=accepted_at,
            updated_at=accepted_at,
        )
        local = accepted.model_copy(
            update={
                "status": TurnStatus.streaming,
                "updated_at": accepted_at + timedelta(seconds=1),
            }
        )
        terminal = accepted.model_copy(
            update={
                "status": TurnStatus.completed,
                "updated_at": accepted_at + timedelta(seconds=2),
            }
        )
        owner_store = SQLiteIdempotencyStore(database)
        await owner_store.claim(
            IdempotencyKey("client-w06", message.session_id, message.message_id),
            fingerprint=message_fingerprint(message),
            state=accepted,
            now=accepted_at,
        )
        race_store = CompletingAfterDuplicateClaimStore(
            database,
            terminal,
            lookup_failure=lookup_failure,
        )
        pipeline = SideEffectPipeline()
        service = TurnService(_logger(), pipeline, idempotency_store=race_store)
        service._remember_existing("client-w06", local)

        with pytest.raises(expected_error):
            await service.accept(message, client_id="client-w06")

        assert pipeline.provider_calls == pipeline.tts_calls == pipeline.playback_calls == 0
        await service.shutdown()

    asyncio.run(scenario())


def test_persisted_terminal_beats_equal_timestamp_streaming_cache_across_services(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "terminal-dominance.sqlite3")
        database.initialize()
        clock = MutableClock()
        winner_pipeline = SideEffectPipeline(wait=True)
        loser_pipeline = SideEffectPipeline()
        winner = TurnService(
            _logger(),
            winner_pipeline,
            idempotency_store=SQLiteIdempotencyStore(database),
            clock=clock,
        )
        loser = TurnService(
            _logger(),
            loser_pipeline,
            idempotency_store=SQLiteIdempotencyStore(database),
            clock=clock,
        )
        message = _message()

        original = await winner.accept(message, client_id="client-w06")
        cached = await loser.accept(message, client_id="client-w06")
        assert cached.turn_id == original.turn_id
        assert cached.status is TurnStatus.streaming

        winner_pipeline.release.set()
        await winner.wait_idle()
        terminal = await loser.accept(message, client_id="client-w06")

        assert terminal.turn_id == original.turn_id
        assert terminal.status is TurnStatus.completed
        assert winner_pipeline.provider_calls == 1
        assert loser_pipeline.provider_calls == 0
        await asyncio.gather(winner.shutdown(), loser.shutdown())

    asyncio.run(scenario())


@given(
    actions=st.lists(
        st.sampled_from(("duplicate", "cancel", "resume", "conflict")),
        min_size=1,
        max_size=25,
    )
)
@settings(max_examples=25, deadline=None)
def test_command_sequence_property_never_reopens_or_reexecutes_a_claimed_turn(
    actions: list[str],
) -> None:
    async def scenario() -> None:
        pipeline = SideEffectPipeline(wait=True)
        observer = CountingObserver()
        vts = CountingVTSSink()
        service = TurnService(
            _logger(),
            pipeline,
            observers=(observer,),
            event_sinks=(vts,),
        )
        message = _message()
        original = await service.accept(message, client_id="client-w06")
        await pipeline.started.wait()
        observed_turn_ids = {original.turn_id}
        cancelled = False

        for action in actions:
            if action == "duplicate":
                duplicate = await service.accept(message, client_id="client-w06")
                observed_turn_ids.add(duplicate.turn_id)
            elif action == "cancel":
                terminal = await service.cancel(
                    client_id="client-w06",
                    session_id="session-w06",
                    turn_id=original.turn_id,
                )
                assert terminal is not None
                observed_turn_ids.add(terminal.turn_id)
                cancelled = True
            elif action == "resume":
                subscription = await service.subscribe(
                    "session-w06",
                    client_id="client-w06",
                    last_seq=0,
                )
                service.unsubscribe(subscription)
            else:
                changed = message.model_copy(update={"text": "同键异文"})
                with pytest.raises(IdempotencyConflictError):
                    await service.accept(changed, client_id="client-w06")

        pipeline.release.set()
        await service.wait_idle()
        terminal_duplicate = await service.accept(message, client_id="client-w06")
        observed_turn_ids.add(terminal_duplicate.turn_id)

        assert observed_turn_ids == {original.turn_id}
        assert pipeline.provider_calls == 1
        assert observer.accepted == 1
        if cancelled:
            assert terminal_duplicate.status is TurnStatus.cancelled
            assert pipeline.tts_calls == pipeline.playback_calls == vts.action_calls == 0
            assert observer.completed == 0
        else:
            assert terminal_duplicate.status is TurnStatus.completed
            assert pipeline.tts_calls == pipeline.playback_calls == vts.action_calls == 1
            assert observer.completed == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_cancel_race_duplicate_and_acceptance_observer_are_linearized_once() -> None:
    async def scenario() -> None:
        pipeline = SideEffectPipeline()
        observer = CountingObserver(gate_acceptance=True)
        service = TurnService(_logger(), pipeline, observers=(observer,))
        message = _message()
        accepting = asyncio.create_task(service.accept(message, client_id="client-w06"))
        await observer.entered.wait()

        duplicate = asyncio.create_task(service.accept(message, client_id="client-w06"))
        first_cancel = asyncio.create_task(
            service.cancel(
                client_id="client-w06",
                session_id="session-w06",
            )
        )
        second_cancel = asyncio.create_task(
            service.cancel(
                client_id="client-w06",
                session_id="session-w06",
            )
        )
        duplicate_state, cancelled_a, cancelled_b = await asyncio.gather(
            duplicate, first_cancel, second_cancel
        )
        observer.release.set()
        original_state = await accepting

        assert original_state.turn_id == duplicate_state.turn_id
        assert cancelled_a is not None and cancelled_b is not None
        assert cancelled_a.turn_id == cancelled_b.turn_id == original_state.turn_id
        assert cancelled_a.status is cancelled_b.status is TurnStatus.cancelled
        assert observer.accepted == 1
        assert observer.completed == 0
        assert pipeline.provider_calls == 0
        await service.shutdown()

    asyncio.run(scenario())


def test_disconnect_resend_replays_terminal_without_repeating_side_effects() -> None:
    async def scenario() -> None:
        pipeline = SideEffectPipeline(wait=True)
        service = TurnService(_logger(), pipeline)
        first_subscription = await service.subscribe(
            "session-w06", client_id="client-w06", last_seq=0
        )
        message = _message()
        accepted = await service.accept(message, client_id="client-w06")
        accepted_event = await _receive_type(first_subscription, "turn.accepted")
        service.unsubscribe(first_subscription)

        pipeline.release.set()
        await service.wait_idle()
        resumed = await service.subscribe(
            "session-w06",
            client_id="client-w06",
            last_seq=accepted_event.seq,
        )
        resent = await service.accept(message, client_id="client-w06")
        completed = await _receive_type(resumed, "assistant.completed")

        assert resent.turn_id == accepted.turn_id == completed.turn_id
        assert resent.status is TurnStatus.completed
        assert pipeline.provider_calls == pipeline.tts_calls == pipeline.playback_calls == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_new_turn_preemption_orders_cancel_before_accept_without_duplicate_preemption() -> None:
    async def scenario() -> None:
        pipeline = SideEffectPipeline(wait=True)
        service = TurnService(_logger(), pipeline)
        subscription = await service.subscribe("session-w06", client_id="client-w06", last_seq=0)
        first = await service.accept(_message(1), client_id="client-w06")
        await pipeline.started.wait()
        pipeline.started.clear()
        second = await service.accept(_message(2), client_id="client-w06")
        duplicate = await service.accept(_message(2), client_id="client-w06")

        observed: list[PipelineEvent] = []
        while not any(
            event.type == "turn.accepted" and event.turn_id == second.turn_id for event in observed
        ):
            item = await asyncio.wait_for(subscription.get(), timeout=2)
            subscription.task_done()
            if isinstance(item, PipelineEvent):
                observed.append(item)
        old_cancel_index = next(
            index
            for index, event in enumerate(observed)
            if event.type == "turn.cancelled" and event.turn_id == first.turn_id
        )
        new_accept_index = next(
            index
            for index, event in enumerate(observed)
            if event.type == "turn.accepted" and event.turn_id == second.turn_id
        )

        assert old_cancel_index < new_accept_index
        assert [event.seq for event in observed] == sorted({event.seq for event in observed})
        assert duplicate.turn_id == second.turn_id
        assert service.snapshot()["active_turns"] == [second.turn_id]
        pipeline.release.set()
        await service.shutdown()

    asyncio.run(scenario())


def test_replay_count_and_time_eviction_return_reset_with_session_only_snapshot() -> None:
    async def scenario() -> None:
        clock = MutableClock()
        service = TurnService(
            _logger(),
            clock=clock,
            replay_limit=3,
            replay_ttl=timedelta(minutes=10),
        )
        for index in range(4):
            state = await service.accept(_message(index), client_id="client-w06")
            await service.cancel(
                client_id="client-w06",
                session_id="session-w06",
                turn_id=state.turn_id,
            )
        reset_subscription = await service.subscribe(
            "session-w06", client_id="client-w06", last_seq=0
        )
        reset = await reset_subscription.get()

        assert isinstance(reset, SessionReset)
        assert reset.reason == "replay_gap"
        assert reset.snapshot.session_id == "session-w06"
        reset_states: list[TurnState] = []
        for _index in range(reset.snapshot.chunk_count):
            chunk = await reset_subscription.get()
            assert isinstance(chunk, SessionSnapshotChunk)
            reset_states.extend(chunk.turns)
        assert {state.session_id for state in reset_states} == {"session-w06"}
        with pytest.raises(IdempotencyAccessError):
            await service.subscribe("session-w06", client_id="client-other", last_seq=0)

        clock.now += timedelta(minutes=11)
        expired = await service.subscribe(
            "session-w06", client_id="client-w06", last_seq=reset.reset_to_seq - 1
        )
        assert isinstance(await expired.get(), SessionReset)

        clock.now += timedelta(hours=24, microseconds=1)
        stale_state_reset = await service.subscribe(
            "session-w06",
            client_id="client-w06",
            last_seq=reset.reset_to_seq + 1,
        )
        stale_state = await stale_state_reset.get()
        assert isinstance(stale_state, SessionReset)
        assert stale_state.snapshot.turn_count == 0
        assert stale_state.snapshot.chunk_count == 0
        await service.shutdown()

    asyncio.run(scenario())


def test_delta_is_coalesced_but_non_droppable_overflow_disconnects_and_recovers() -> None:
    async def scenario() -> None:
        class DeltaOnlyPipeline(SideEffectPipeline):
            async def run(
                self,
                _message: UserMessage,
                _state: TurnState,
                _token: CancellationToken,
                emit: Callable[[str, dict[str, Any]], Awaitable[None]],
            ) -> TurnOutcome:
                self.provider_calls += 1
                self.started.set()
                for _index in range(self.delta_count):
                    await emit("assistant.delta", {"delta": "爆"})
                return TurnOutcome(
                    full_text="爆" * self.delta_count,
                    segments=[],
                    metrics=TurnMetrics(),
                )

        pipeline = DeltaOnlyPipeline(delta_count=100)
        service = TurnService(_logger(), pipeline, subscriber_queue_limit=3)
        merged = await service.subscribe("session-w06", client_id="client-w06", last_seq=0)
        await service.accept(_message(), client_id="client-w06")
        await service.wait_idle()

        items = [await merged.get() for _index in range(3)]
        deltas = [
            item
            for item in items
            if isinstance(item, PipelineEvent) and item.type == "assistant.delta"
        ]
        assert len(deltas) == 1
        assert deltas[0].payload["delta"] == "爆" * 100
        assert deltas[0].payload["coalesced_event_count"] == 100

        overflow_service = TurnService(_logger(), subscriber_queue_limit=1)
        slow = await overflow_service.subscribe("session-w06", client_id="client-w06", last_seq=0)
        accepted = await overflow_service.accept(_message(1), client_id="client-w06")
        await overflow_service.cancel(
            client_id="client-w06",
            session_id="session-w06",
            turn_id=accepted.turn_id,
        )
        with pytest.raises(SlowConsumerError):
            await slow.get()
        assert slow.closed_reason == "slow_consumer"

        recovered = await overflow_service.subscribe(
            "session-w06", client_id="client-w06", last_seq=0
        )
        replayed = await recovered.get()
        assert isinstance(replayed, PipelineEvent)
        assert replayed.type == "turn.accepted"
        await service.shutdown()
        await overflow_service.shutdown()

    asyncio.run(scenario())


def test_10k_turns_terminal_ttl_lru_and_replay_memory_reach_a_stable_bound() -> None:
    async def scenario() -> None:
        clock = MutableClock()
        store = InMemoryIdempotencyStore(clock=clock)
        service = TurnService(
            _logger(),
            idempotency_store=store,
            clock=clock,
        )
        for index in range(10_000):
            state = await service.accept(_message(index), client_id="client-w06")
            terminal = await service.cancel(
                client_id="client-w06",
                session_id="session-w06",
                turn_id=state.turn_id,
            )
            assert terminal is not None

        snapshot = service.snapshot()
        assert len(snapshot["turns"]) == 200
        assert snapshot["replay_event_count"] == 2_000
        assert await store.count_records(client_id="client-w06", session_id="session-w06") == 200

        clock.now += timedelta(hours=24, microseconds=1)
        next_state = await service.accept(_message(10_001), client_id="client-w06")
        await service.cancel(
            client_id="client-w06",
            session_id="session-w06",
            turn_id=next_state.turn_id,
        )
        assert len(service.snapshot()["turns"]) == 1
        assert await store.count_records(client_id="client-w06", session_id="session-w06") == 1
        await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_terminal_cancel_refreshes_storage_lru_before_capacity_pruning(
    tmp_path: Path,
    backend: str,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        pipeline = SideEffectPipeline()
        store: IdempotencyStore
        if backend == "memory":
            store = InMemoryIdempotencyStore(clock=clock, terminal_limit=2)
        else:
            database = SQLiteDatabase(tmp_path / "lru-divergence.sqlite3")
            database.initialize()
            store = SQLiteIdempotencyStore(database, terminal_limit=2)
        service = TurnService(
            _logger(),
            pipeline,
            idempotency_store=store,
            clock=clock,
            terminal_limit=2,
        )

        first = await service.accept(_message(1), client_id="client-w06")
        await service.wait_idle()
        await service.accept(_message(2), client_id="client-w06")
        await service.wait_idle()
        touched = await service.cancel(
            client_id="client-w06",
            session_id="session-w06",
            turn_id=first.turn_id,
        )
        assert touched is not None and touched.turn_id == first.turn_id

        await service.accept(_message(3), client_id="client-w06")
        await service.wait_idle()
        duplicate = await service.accept(_message(1), client_id="client-w06")

        assert duplicate.turn_id == first.turn_id
        assert pipeline.provider_calls == 3
        await service.shutdown()

    asyncio.run(scenario())


def test_restart_hydrates_cancel_and_chunked_authoritative_snapshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "restart.sqlite3")
        database.initialize()
        first_store = SQLiteIdempotencyStore(database)
        first = TurnService(_logger(), idempotency_store=first_store)
        state = await first.accept(_message(), client_id="client-w06")
        terminal = await first.cancel(
            client_id="client-w06",
            session_id="session-w06",
            turn_id=state.turn_id,
        )
        assert terminal is not None and terminal.status is TurnStatus.cancelled
        await first.shutdown()

        restarted = TurnService(
            _logger(),
            idempotency_store=SQLiteIdempotencyStore(database),
        )
        subscription = await restarted.subscribe(
            "session-w06",
            client_id="client-w06",
            last_seq=999,
        )
        reset = await subscription.get()
        subscription.task_done()
        chunk = await subscription.get()
        subscription.task_done()

        assert isinstance(reset, SessionReset)
        assert reset.snapshot.turn_count == 1
        assert reset.snapshot.chunk_count == 1
        assert isinstance(chunk, SessionSnapshotChunk)
        assert chunk.reset_id == reset.reset_id
        assert chunk.turns == [terminal]
        recovered_cancel = await restarted.cancel(
            client_id="client-w06",
            session_id="session-w06",
        )
        assert recovered_cancel == terminal
        await restarted.shutdown()

    asyncio.run(scenario())


def test_no_pipeline_keeps_state_bounded_and_reset_chunks_at_most_50_turns() -> None:
    async def scenario() -> None:
        service = TurnService(_logger())
        for index in range(250):
            await service.accept(_message(index), client_id="client-w06")

        state = service.snapshot()
        assert len(state["turns"]) == 201
        assert len(state["active_turns"]) == 1
        subscription = await service.subscribe(
            "session-w06",
            client_id="client-w06",
            last_seq=99_999,
        )
        reset = await subscription.get()
        subscription.task_done()
        assert isinstance(reset, SessionReset)
        chunks = []
        for _index in range(reset.snapshot.chunk_count):
            item = await subscription.get()
            subscription.task_done()
            assert isinstance(item, SessionSnapshotChunk)
            chunks.append(item)

        assert reset.snapshot.turn_count == 201
        assert sum(len(chunk.turns) for chunk in chunks) == 201
        assert all(len(chunk.turns) <= 50 for chunk in chunks)
        assert {chunk.reset_id for chunk in chunks} == {reset.reset_id}
        await service.shutdown()

    asyncio.run(scenario())


def test_slow_consumer_close_signal_does_not_wait_for_blocked_sender() -> None:
    async def scenario() -> None:
        service = TurnService(_logger(), subscriber_queue_limit=1)
        slow = await service.subscribe("session-w06", client_id="client-w06", last_seq=0)
        state = await service.accept(_message(), client_id="client-w06")
        await service.cancel(
            client_id="client-w06",
            session_id="session-w06",
            turn_id=state.turn_id,
        )

        assert await asyncio.wait_for(slow.wait_closed(), timeout=0.1) == "slow_consumer"
        await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "override",
    [
        {"replay_limit": 2_001},
        {"replay_ttl": timedelta(minutes=10, microseconds=1)},
        {"terminal_limit": 201},
        {"terminal_ttl": timedelta(hours=24, microseconds=1)},
        {"subscriber_queue_limit": 513},
    ],
)
def test_resource_limits_can_be_tightened_but_not_relaxed(override: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="hard limits"):
        TurnService(_logger(), **override)


def test_cross_session_cancel_is_rejected_and_storage_failure_is_fail_closed() -> None:
    async def scenario() -> None:
        service = TurnService(_logger())
        state = await service.accept(_message(), client_id="client-w06")
        with pytest.raises(TurnAccessError):
            await service.cancel(
                client_id="client-w06",
                session_id="session-other",
                turn_id=state.turn_id,
            )
        await service.shutdown()

        pipeline = SideEffectPipeline()
        observer = CountingObserver()
        failed = TurnService(
            _logger(),
            pipeline,
            observers=(observer,),
            idempotency_store=UnavailableIdempotencyStore(),
        )
        with pytest.raises(RuntimeError, match="idempotency_unavailable"):
            await failed.accept(_message(), client_id="client-w06")
        assert failed.snapshot()["turns"] == {}
        assert pipeline.provider_calls == 0
        assert observer.accepted == observer.completed == 0
        await failed.shutdown()

    asyncio.run(scenario())


def test_running_persistence_failure_stops_before_provider_and_duplicate_stays_failed() -> None:
    async def scenario() -> None:
        pipeline = SideEffectPipeline()
        store = FaultInjectingMemoryStore({TurnStatus.streaming: 1})
        service = TurnService(_logger(), pipeline, idempotency_store=store)
        message = _message()

        original = await service.accept(message, client_id="client-w06")
        await service.wait_idle()
        duplicate = await service.accept(message, client_id="client-w06")

        assert duplicate.turn_id == original.turn_id
        assert duplicate.status is TurnStatus.failed
        assert duplicate.error_code == "idempotency_unavailable"
        assert pipeline.provider_calls == 0
        assert pipeline.tts_calls == pipeline.playback_calls == 0
        await service.shutdown()

    asyncio.run(scenario())


def test_terminal_persistence_failure_repairs_or_recovers_without_reexecution() -> None:
    async def scenario() -> None:
        transient_pipeline = SideEffectPipeline()
        transient_store = FaultInjectingMemoryStore({TurnStatus.completed: 1})
        transient = TurnService(
            _logger(),
            transient_pipeline,
            idempotency_store=transient_store,
        )
        message = _message(1)
        original = await transient.accept(message, client_id="client-w06")
        await transient.wait_idle()
        repaired = await transient.accept(message, client_id="client-w06")

        assert repaired.turn_id == original.turn_id
        assert repaired.status is TurnStatus.completed
        assert transient_pipeline.provider_calls == 1
        await transient.shutdown()

        persistent_pipeline = SideEffectPipeline()
        persistent_store = FaultInjectingMemoryStore({TurnStatus.completed: -1})
        persistent = TurnService(
            _logger(),
            persistent_pipeline,
            idempotency_store=persistent_store,
        )
        persistent_message = _message(2)
        persisted = await persistent.accept(persistent_message, client_id="client-w06")
        await persistent.wait_idle()
        with pytest.raises(IdempotencyUnavailableError):
            await persistent.accept(persistent_message, client_id="client-w06")
        assert persistent_pipeline.provider_calls == 1
        await persistent.shutdown()

        assert await persistent_store.recover_incomplete(now=datetime.now(UTC)) == 1
        restarted_pipeline = SideEffectPipeline()
        restarted = TurnService(
            _logger(),
            restarted_pipeline,
            idempotency_store=persistent_store,
        )
        recovered = await restarted.accept(persistent_message, client_id="client-w06")
        assert recovered.turn_id == persisted.turn_id
        assert recovered.status is TurnStatus.failed
        assert recovered.error_code == "service_restarted"
        assert restarted_pipeline.provider_calls == 0
        await restarted.shutdown()

    asyncio.run(scenario())
