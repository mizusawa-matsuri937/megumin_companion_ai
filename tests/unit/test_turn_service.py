"""Turn lifecycle, observer isolation, cancellation, and error mapping tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import pytest
from app.core import CancellationToken, TurnService
from app.schemas import (
    ProactiveIntent,
    TurnMetrics,
    TurnOutcome,
    TurnState,
    TurnStatus,
    UserMessage,
)


class PipelineErrorCode(StrEnum):
    unavailable = "provider_unavailable"


class PipelineFailure(RuntimeError):
    def __init__(self, code: PipelineErrorCode | str | None = None) -> None:
        super().__init__("private upstream failure")
        self.code = code


class ControllablePipeline:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        wait_forever: bool = False,
    ) -> None:
        self.failure = failure
        self.wait_forever = wait_forever
        self.started = asyncio.Event()
        self.proactive_started = asyncio.Event()
        self.closed = False
        self.close_calls = 0
        self.run_calls: list[str] = []
        self.active_runs = 0
        self.max_active_runs = 0

    async def run(
        self,
        _message: UserMessage,
        _state: TurnState,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        self.started.set()
        self.run_calls.append(_state.session_id)
        return await self._execute(token, emit)

    async def run_proactive(
        self,
        _intent: ProactiveIntent,
        _state: TurnState,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        self.proactive_started.set()
        self.run_calls.append("proactive")
        return await self._execute(token, emit)

    async def _execute(
        self,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        self.active_runs += 1
        self.max_active_runs = max(self.max_active_runs, self.active_runs)
        try:
            await emit("assistant.delta", {"delta": "完成"})
            if self.wait_forever:
                await token.wait()
                token.raise_if_cancelled()
            if self.failure is not None:
                raise self.failure
            return TurnOutcome(
                full_text="完成",
                segments=[],
                metrics=TurnMetrics(turn_total_ms=1),
            )
        finally:
            self.active_runs -= 1

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class RecordingObserver:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.accepted: list[str] = []
        self.completed: list[str] = []

    async def on_user_accepted(self, _message: UserMessage, state: TurnState) -> None:
        self.accepted.append(state.turn_id)
        if self.fail:
            raise RuntimeError("observer input must stay isolated")

    async def on_turn_completed(
        self,
        _message: UserMessage,
        state: TurnState,
        _outcome: TurnOutcome,
    ) -> None:
        self.completed.append(state.turn_id)
        if self.fail:
            raise RuntimeError("observer output must stay isolated")


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[str] = []
        self.closed = False

    def publish(self, event: Any) -> bool:
        self.events.append(event.type)
        if self.fail:
            raise RuntimeError("event sink must stay isolated")
        return True

    async def close(self) -> None:
        self.closed = True


class GatedObserver(RecordingObserver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def on_user_accepted(self, message: UserMessage, state: TurnState) -> None:
        await super().on_user_accepted(message, state)
        self.entered.set()
        await self.release.wait()


class CompletionGateObserver(RecordingObserver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def on_turn_completed(
        self,
        message: UserMessage,
        state: TurnState,
        outcome: TurnOutcome,
    ) -> None:
        await super().on_turn_completed(message, state, outcome)
        self.entered.set()
        await self.release.wait()


class RecordingPriority:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.snapshots: list[frozenset[str]] = []

    async def begin_user_turn(self, turn_id: str) -> None:
        self.active.add(turn_id)
        self.snapshots.append(frozenset(self.active))

    async def end_user_turn(self, turn_id: str) -> None:
        self.active.discard(turn_id)
        self.snapshots.append(frozenset(self.active))


def logger() -> logging.Logger:
    instance = logging.getLogger("test.turn_service")
    instance.handlers = [logging.NullHandler()]
    instance.propagate = False
    return instance


async def receive_type(
    queue: asyncio.Queue[Any],
    event_type: str,
) -> Any:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=1)
        queue.task_done()
        if event.type == event_type:
            return event


def test_success_records_safe_outcome_and_observers_are_isolated() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline()
        good = RecordingObserver()
        bad = RecordingObserver(fail=True)
        good_sink = RecordingSink()
        bad_sink = RecordingSink(fail=True)
        service = TurnService(
            logger(),
            pipeline,
            observers=(bad, good),
            event_sinks=(bad_sink, good_sink),
        )
        queue = service.subscribe("session-a")
        wildcard = service.subscribe("*")

        state = await service.accept(UserMessage(text="你好", session_id="session-a"))
        event = await receive_type(queue, "assistant.completed")
        wildcard_event = await receive_type(wildcard, "assistant.completed")

        assert event.turn_id == state.turn_id == wildcard_event.turn_id
        assert service.snapshot()["turns"][state.turn_id]["status"] == "completed"
        assert service.snapshot()["metrics"][state.turn_id]["turn_total_ms"] == 1
        assert service.snapshot()["outcomes"][state.turn_id] == {
            "text_length": 2,
            "segment_count": 0,
        }
        assert good.accepted == good.completed == [state.turn_id]
        assert bad.accepted == bad.completed == [state.turn_id]
        assert good_sink.events == bad_sink.events
        assert good_sink.events[0] == "turn.accepted"
        assert good_sink.events[-1] == "assistant.completed"

        service.unsubscribe("session-a", queue)
        service.unsubscribe("session-a", queue)
        service.unsubscribe("*", wildcard)
        assert service.snapshot()["subscriber_count"] == 0
        assert (await service.cancel(turn_id=state.turn_id)) is not None
        await service.shutdown()
        await service.shutdown()
        assert pipeline.closed
        assert good_sink.closed and bad_sink.closed
        with pytest.raises(RuntimeError, match="已关闭"):
            await service.accept(UserMessage(text="关闭后输入"))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (PipelineFailure(PipelineErrorCode.unavailable), "provider_unavailable"),
        (PipelineFailure("custom_failure"), "custom_failure"),
        (RuntimeError("generic"), "pipeline_failed"),
    ],
)
def test_failure_codes_are_safe_and_terminal(failure: Exception, expected: str) -> None:
    async def scenario() -> None:
        service = TurnService(logger(), ControllablePipeline(failure=failure))
        queue = service.subscribe("local_session")
        state = await service.accept(UserMessage(text="触发失败"))
        event = await receive_type(queue, "turn.failed")

        assert event.payload["error_code"] == expected
        terminal = await service.cancel(turn_id=state.turn_id)
        assert terminal is not None and terminal.status is TurnStatus.failed
        await service.shutdown()

    asyncio.run(scenario())


def test_cancel_without_pipeline_and_unknown_turns() -> None:
    async def scenario() -> None:
        service = TurnService(logger())
        queue = service.subscribe("local_session")
        state = await service.accept(UserMessage(text="只接受，不运行"))
        cancelled = await service.cancel(turn_id=state.turn_id)

        assert cancelled is not None and cancelled.status is TurnStatus.cancelled
        assert (await receive_type(queue, "turn.cancelled")).turn_id == state.turn_id
        assert await service.cancel(turn_id="turn_missing") is None
        assert await service.cancel(session_id="missing-session") is None
        await service.shutdown()

    asyncio.run(scenario())


def test_cancel_active_pipeline_and_shutdown_cleanup() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        service = TurnService(logger(), pipeline)
        queue = service.subscribe("local_session")
        state = await service.accept(UserMessage(text="长任务"))
        await pipeline.started.wait()
        cancelled = await service.cancel(turn_id=state.turn_id, reason="test")

        assert cancelled is not None and cancelled.status is TurnStatus.cancelled
        assert (await receive_type(queue, "turn.cancelled")).turn_id == state.turn_id

        pipeline.started.clear()
        second = await service.accept(UserMessage(text="由 shutdown 取消"))
        await pipeline.started.wait()
        await service.shutdown()
        assert service.snapshot()["turns"][second.turn_id]["status"] == "cancelled"
        assert pipeline.closed

    asyncio.run(scenario())


def test_interrupt_during_acceptance_does_not_wait_for_observer() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        observer = GatedObserver()
        service = TurnService(logger(), pipeline, observers=(observer,))
        queue = service.subscribe("local_session")

        accepting = asyncio.create_task(service.accept(UserMessage(text="原子注册")))
        accepted = await receive_type(queue, "turn.accepted")
        await observer.entered.wait()
        interrupting = asyncio.create_task(service.cancel(turn_id=accepted.turn_id))
        await asyncio.sleep(0)
        assert interrupting.done()

        cancelled = await interrupting
        assert cancelled is not None and cancelled.status is TurnStatus.cancelled
        assert not pipeline.started.is_set()

        observer.release.set()
        state = await accepting
        assert state.turn_id == accepted.turn_id
        assert state.status is TurnStatus.cancelled
        assert service.snapshot()["active_turns"] == []
        await service.shutdown()

    asyncio.run(scenario())


def test_cancelling_acceptance_cleans_registered_state_and_priority() -> None:
    async def scenario() -> None:
        observer = GatedObserver()
        priority = RecordingPriority()
        service = TurnService(
            logger(),
            ControllablePipeline(wait_forever=True),
            observers=(observer,),
            priority_controller=priority,
        )
        queue = service.subscribe("local_session")
        accepting = asyncio.create_task(service.accept(UserMessage(text="取消接受过程")))
        accepted = await receive_type(queue, "turn.accepted")
        await observer.entered.wait()

        accepting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await accepting

        snapshot = service.snapshot()
        assert snapshot["turns"][accepted.turn_id]["status"] == "cancelled"
        assert snapshot["active_turns"] == []
        assert priority.active == set()
        assert await service.cancel(session_id="local_session") is None
        await service.shutdown()

    asyncio.run(scenario())


def test_interrupt_returns_during_success_observer_after_atomic_completion_commit() -> None:
    async def scenario() -> None:
        observer = CompletionGateObserver()
        service = TurnService(
            logger(),
            ControllablePipeline(),
            observers=(observer,),
        )
        state = await service.accept(UserMessage(text="完成边界"))
        await observer.entered.wait()

        interrupting = asyncio.create_task(service.cancel(turn_id=state.turn_id))
        await asyncio.sleep(0)
        assert interrupting.done()
        assert service.snapshot()["turns"][state.turn_id]["status"] == "completed"

        terminal = await interrupting
        assert terminal is not None and terminal.status is TurnStatus.completed
        assert service.snapshot()["active_turns"] == []

        observer.release.set()
        await service.shutdown()

    asyncio.run(scenario())


def test_replacement_turn_keeps_priority_marker_without_a_gap() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        priority = RecordingPriority()
        service = TurnService(logger(), pipeline, priority_controller=priority)

        first = await service.accept(UserMessage(text="第一轮"))
        second = await service.accept(UserMessage(text="第二轮"))

        assert priority.active == {second.turn_id}
        assert any(
            snapshot == frozenset({first.turn_id, second.turn_id})
            for snapshot in priority.snapshots
        )
        assert frozenset() not in priority.snapshots[:-1]
        await service.shutdown()
        assert priority.active == set()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["cancel", "shutdown"])
def test_immediate_terminal_operation_cannot_leave_an_unstarted_task(
    operation: str,
) -> None:
    async def scenario() -> None:
        priority = RecordingPriority()
        service = TurnService(
            logger(),
            ControllablePipeline(wait_forever=True),
            priority_controller=priority,
        )
        state = await service.accept(UserMessage(text="立即终止"))

        if operation == "cancel":
            await service.cancel(turn_id=state.turn_id)
            await service.shutdown()
        else:
            await service.shutdown()

        assert service.snapshot()["turns"][state.turn_id]["status"] == "cancelled"
        assert service.snapshot()["active_turns"] == []
        assert priority.active == set()

    asyncio.run(scenario())


def test_cross_session_replacement_is_globally_serialized() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        service = TurnService(logger(), pipeline)

        first = await service.accept(UserMessage(text="第一路", session_id="session-a"))
        await pipeline.started.wait()
        pipeline.started.clear()
        second = await service.accept(UserMessage(text="第二路", session_id="session-b"))
        await pipeline.started.wait()

        snapshot = service.snapshot()
        assert snapshot["turns"][first.turn_id]["status"] == "cancelled"
        assert snapshot["turns"][second.turn_id]["status"] == "streaming"
        assert pipeline.run_calls == ["session-a", "session-b"]
        assert pipeline.max_active_runs == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_acceptance_observer_can_reenter_cancel_without_deadlock() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        service: TurnService

        class CancellingObserver(RecordingObserver):
            async def on_user_accepted(self, message: UserMessage, state: TurnState) -> None:
                await super().on_user_accepted(message, state)
                cancelled = await service.cancel(turn_id=state.turn_id)
                assert cancelled is not None and cancelled.status is TurnStatus.cancelled

        observer = CancellingObserver()
        service = TurnService(logger(), pipeline, observers=(observer,))
        state = await asyncio.wait_for(service.accept(UserMessage(text="重入取消")), timeout=1)

        assert service.snapshot()["turns"][state.turn_id]["status"] == "cancelled"
        assert not pipeline.started.is_set()
        assert service.snapshot()["active_turns"] == []
        await service.shutdown()

    asyncio.run(scenario())


def test_completion_observer_can_reenter_cancel_without_deadlock() -> None:
    async def scenario() -> None:
        service: TurnService

        class CancellingObserver(RecordingObserver):
            async def on_turn_completed(
                self,
                message: UserMessage,
                state: TurnState,
                outcome: TurnOutcome,
            ) -> None:
                await super().on_turn_completed(message, state, outcome)
                terminal = await service.cancel(turn_id=state.turn_id)
                assert terminal is not None and terminal.status is TurnStatus.completed

        observer = CancellingObserver()
        service = TurnService(logger(), ControllablePipeline(), observers=(observer,))
        queue = service.subscribe("local_session")
        state = await service.accept(UserMessage(text="完成后重入"))
        await receive_type(queue, "assistant.completed")

        assert observer.completed == [state.turn_id]
        assert service.snapshot()["active_turns"] == []
        await service.shutdown()

    asyncio.run(scenario())


def test_explicit_input_atomically_preempts_proactive_without_observer_pollution() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        observer = RecordingObserver()
        service = TurnService(logger(), pipeline, observers=(observer,))
        token = CancellationToken("proactive-test")
        proactive = asyncio.create_task(
            service.run_proactive(
                ProactiveIntent(
                    trigger_type="idle",
                    instruction="发起一次简短问候",
                    score=0.8,
                    reason="test",
                ),
                token,
            )
        )
        await pipeline.proactive_started.wait()

        user = await service.accept(UserMessage(text="用户现在说话", session_id="session-user"))
        with pytest.raises(asyncio.CancelledError):
            await proactive

        assert token.cancelled
        assert observer.accepted == [user.turn_id]
        assert pipeline.run_calls == ["proactive", "session-user"]
        assert pipeline.max_active_runs == 1
        assert service.snapshot()["outcomes"] == {}
        await service.shutdown()

    asyncio.run(scenario())


def test_concurrent_shutdown_closes_every_resource_once_even_after_failure() -> None:
    async def scenario() -> None:
        class FailingPipeline(ControllablePipeline):
            async def close(self) -> None:
                await super().close()
                raise RuntimeError("private pipeline close failure")

        pipeline = FailingPipeline()
        good_sink = RecordingSink()
        service = TurnService(logger(), pipeline, event_sinks=(good_sink,))

        await asyncio.gather(service.shutdown(), service.shutdown(), service.shutdown())

        assert pipeline.closed and pipeline.close_calls == 1
        assert good_sink.closed

    asyncio.run(scenario())


def test_acceptance_observer_can_reenter_accept_without_self_deadlock() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        service: TurnService

        class ReentrantObserver(RecordingObserver):
            nested: TurnState | None = None
            reentered = False

            async def on_user_accepted(self, message: UserMessage, state: TurnState) -> None:
                await super().on_user_accepted(message, state)
                if not self.reentered:
                    self.reentered = True
                    self.nested = await service.accept(
                        UserMessage(text="observer 内的新输入", session_id="nested-session")
                    )

        observer = ReentrantObserver()
        service = TurnService(logger(), pipeline, observers=(observer,))
        outer = await asyncio.wait_for(
            service.accept(UserMessage(text="外层输入", session_id="outer-session")),
            timeout=1,
        )

        assert outer.status is TurnStatus.cancelled
        assert observer.nested is not None
        assert service.snapshot()["active_turns"] == [observer.nested.turn_id]
        await service.shutdown()

    asyncio.run(scenario())


def test_completion_observer_can_reenter_accept_without_self_join() -> None:
    async def scenario() -> None:
        service: TurnService

        class ReentrantObserver(RecordingObserver):
            reentered = False

            async def on_turn_completed(
                self,
                message: UserMessage,
                state: TurnState,
                outcome: TurnOutcome,
            ) -> None:
                await super().on_turn_completed(message, state, outcome)
                if not self.reentered:
                    self.reentered = True
                    await service.accept(
                        UserMessage(text="完成 observer 的新输入", session_id="nested-session")
                    )

        observer = ReentrantObserver()
        service = TurnService(logger(), ControllablePipeline(), observers=(observer,))
        queue = service.subscribe("*")
        first = await service.accept(UserMessage(text="第一轮", session_id="first-session"))
        await receive_type(queue, "assistant.completed")
        await receive_type(queue, "assistant.completed")

        assert observer.reentered
        assert service.snapshot()["turns"][first.turn_id]["status"] == "completed"
        assert service.snapshot()["active_turns"] == []
        await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["acceptance", "completion"])
def test_observer_can_initiate_shutdown_without_an_owned_task_cycle(phase: str) -> None:
    async def scenario() -> None:
        service: TurnService

        class ShutdownObserver(RecordingObserver):
            async def on_user_accepted(self, message: UserMessage, state: TurnState) -> None:
                await super().on_user_accepted(message, state)
                if phase == "acceptance":
                    await service.shutdown()

            async def on_turn_completed(
                self,
                message: UserMessage,
                state: TurnState,
                outcome: TurnOutcome,
            ) -> None:
                await super().on_turn_completed(message, state, outcome)
                if phase == "completion":
                    await service.shutdown()

        pipeline = ControllablePipeline()
        service = TurnService(logger(), pipeline, observers=(ShutdownObserver(),))
        state = await asyncio.wait_for(service.accept(UserMessage(text="触发关闭")), timeout=1)
        await asyncio.wait_for(service.shutdown(), timeout=1)

        expected = TurnStatus.cancelled if phase == "acceptance" else TurnStatus.completed
        assert service.snapshot()["turns"][state.turn_id]["status"] == expected.value
        assert pipeline.closed and pipeline.close_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_by_id", [False, True])
def test_proactive_turn_uses_the_same_direct_cancel_registry(cancel_by_id: bool) -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        service = TurnService(logger(), pipeline)
        queue = service.subscribe("local_session")
        token = CancellationToken("proactive-direct-cancel")
        proactive = asyncio.create_task(
            service.run_proactive(
                ProactiveIntent(
                    trigger_type="idle",
                    instruction="主动问候",
                    score=0.8,
                    reason="test",
                ),
                token,
            )
        )
        accepted = await receive_type(queue, "proactive.accepted")
        await pipeline.proactive_started.wait()

        terminal = await service.cancel(
            turn_id=accepted.turn_id if cancel_by_id else None,
            session_id="local_session",
        )
        with pytest.raises(asyncio.CancelledError):
            await proactive

        assert terminal is not None and terminal.status is TurnStatus.cancelled
        assert token.cancelled
        assert service.snapshot()["turns"][accepted.turn_id]["status"] == "cancelled"
        assert service.snapshot()["active_turns"] == []
        await service.shutdown()

    asyncio.run(scenario())


def test_duplicate_cancel_joins_one_cleanup_without_cancelling_it_twice() -> None:
    async def scenario() -> None:
        class CleanupGatePipeline(ControllablePipeline):
            def __init__(self) -> None:
                super().__init__()
                self.cleanup_started = asyncio.Event()
                self.cleanup_release = asyncio.Event()
                self.cleanup_finished = False

            async def run(
                self,
                _message: UserMessage,
                _state: TurnState,
                _token: CancellationToken,
                _emit: Callable[[str, dict[str, Any]], Awaitable[None]],
            ) -> TurnOutcome:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                    raise AssertionError("unreachable cleanup gate")
                finally:
                    self.cleanup_started.set()
                    await self.cleanup_release.wait()
                    self.cleanup_finished = True

        pipeline = CleanupGatePipeline()
        service = TurnService(logger(), pipeline)
        state = await service.accept(UserMessage(text="需要清理"))
        first = asyncio.create_task(service.cancel(turn_id=state.turn_id))
        await pipeline.cleanup_started.wait()
        second = asyncio.create_task(service.cancel(turn_id=state.turn_id))
        await asyncio.sleep(0)

        assert not first.done() and not second.done()
        pipeline.cleanup_release.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result is not None and first_result.status is TurnStatus.cancelled
        assert second_result is not None and second_result.status is TurnStatus.cancelled
        assert pipeline.cleanup_finished
        await service.shutdown()

    asyncio.run(scenario())


def test_shutdown_waits_for_inflight_acceptance_before_closing_dependencies() -> None:
    async def scenario() -> None:
        pipeline = ControllablePipeline(wait_forever=True)
        observer = GatedObserver()
        service = TurnService(logger(), pipeline, observers=(observer,))
        accepting = asyncio.create_task(service.accept(UserMessage(text="持久化中")))
        await observer.entered.wait()
        closing = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)

        assert not closing.done()
        assert not pipeline.closed
        observer.release.set()
        accepted = await accepting
        await closing

        assert accepted.status is TurnStatus.cancelled
        assert service.snapshot()["active_turns"] == []
        assert pipeline.closed

    asyncio.run(scenario())
