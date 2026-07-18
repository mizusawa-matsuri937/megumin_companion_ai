from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core import CancellationToken, TurnService
from app.core.idempotency import (
    IdempotencyAccessError,
    IdempotencyConflictError,
    InMemoryIdempotencyStore,
    UnavailableIdempotencyStore,
)
from app.core.turns import SlowConsumerError, TurnAccessError
from app.schemas import (
    PipelineEvent,
    SessionReset,
    TurnMetrics,
    TurnOutcome,
    TurnState,
    TurnStatus,
    UserMessage,
)
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
        assert {state.session_id for state in reset.snapshot.turns} == {"session-w06"}
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
        assert stale_state.snapshot.turns == []
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
