"""Single dialogue entry point shared by text and transcribed voice messages."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from enum import Enum
from typing import Any, Protocol

from app.config.logging import log_event
from app.core.cancellation import CancellationToken
from app.core.contracts import TurnEventSink, TurnPriorityController
from app.schemas import (
    PipelineEvent,
    TurnOutcome,
    TurnState,
    TurnStatus,
    UserMessage,
    utc_now,
)


class TurnPipeline(Protocol):
    async def run(
        self,
        message: UserMessage,
        state: TurnState,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome: ...

    async def close(self) -> None: ...


class TurnObserver(Protocol):
    async def on_user_accepted(self, message: UserMessage, state: TurnState) -> None: ...

    async def on_turn_completed(
        self,
        message: UserMessage,
        state: TurnState,
        outcome: TurnOutcome,
    ) -> None: ...


class TurnService:
    """Manage turn isolation, event delivery, cancellation, and shutdown cleanup."""

    def __init__(
        self,
        logger: logging.Logger,
        pipeline: TurnPipeline | None = None,
        *,
        observers: Sequence[TurnObserver] = (),
        event_sinks: Sequence[TurnEventSink] = (),
        priority_controller: TurnPriorityController | None = None,
    ) -> None:
        self._logger = logger
        self._pipeline = pipeline
        self._states: dict[str, TurnState] = {}
        self._outcomes: dict[str, TurnOutcome] = {}
        self._observers = tuple(observers)
        self._event_sinks = tuple(event_sinks)
        self._priority_controller = priority_controller
        self._tokens: dict[str, CancellationToken] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._session_turn: dict[str, str] = {}
        self._subscribers: dict[str, set[asyncio.Queue[PipelineEvent]]] = {}
        self._closed = False
        self._coordination_lock = asyncio.Lock()

    async def accept(self, message: UserMessage) -> TurnState:
        async with self._coordination_lock:
            if self._closed:
                raise RuntimeError("TurnService 已关闭")
            state = TurnState(
                session_id=message.session_id,
                source_message_id=message.message_id,
                input_mode=message.input_mode,
            )
            priority_owned = False
            registered = False
            token: CancellationToken | None = None
            task: asyncio.Task[None] | None = None
            try:
                # Acquire cleanup ownership before the controller can add its
                # marker; begin_user_turn may itself be cancelled while waiting
                # for proactive cleanup.
                priority_owned = True
                await self._begin_user_priority(state.turn_id)
                # The priority marker for this new turn is established before the
                # previous marker can disappear, leaving no proactive-start gap.
                await self._cancel_locked(
                    session_id=message.session_id,
                    turn_id=None,
                    reason="superseded",
                )
                self._states[state.turn_id] = state
                self._session_turn[message.session_id] = state.turn_id
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
                await self._publish(
                    PipelineEvent(
                        type="turn.accepted",
                        turn_id=state.turn_id,
                        session_id=state.session_id,
                        payload=state.model_dump(mode="json"),
                    )
                )
                await self._notify_user_accepted(message, state)

                if self._pipeline is not None:
                    token = CancellationToken(state.turn_id)
                    started = asyncio.Event()
                    self._tokens[state.turn_id] = token
                    task = asyncio.create_task(
                        self._run(message, state, token, started),
                        name=f"dialogue-{state.turn_id}",
                    )
                    self._tasks[state.turn_id] = task
                    # Do not transfer cleanup ownership until the task has
                    # entered a cancellation-protected try/finally region.
                    await started.wait()
                    priority_owned = False
                else:
                    self._session_turn.pop(message.session_id, None)
                return state
            except BaseException:
                if task is not None:
                    if token is not None:
                        token.cancel()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if registered:
                    current = self._states[state.turn_id]
                    if current.status not in {
                        TurnStatus.cancelled,
                        TurnStatus.completed,
                        TurnStatus.failed,
                    }:
                        self._set_status(state.turn_id, TurnStatus.cancelled)
                        await self._publish_state_event(state.turn_id, "turn.cancelled")
                    self._tokens.pop(state.turn_id, None)
                    self._tasks.pop(state.turn_id, None)
                    if self._session_turn.get(state.session_id) == state.turn_id:
                        self._session_turn.pop(state.session_id, None)
                raise
            finally:
                if priority_owned:
                    await self._end_user_priority(state.turn_id)

    async def _run(
        self,
        message: UserMessage,
        state: TurnState,
        token: CancellationToken,
        started: asyncio.Event,
    ) -> None:
        assert self._pipeline is not None

        async def emit(event_type: str, payload: dict[str, Any]) -> None:
            token.raise_if_cancelled()
            await self._publish(
                PipelineEvent(
                    type=event_type,
                    turn_id=state.turn_id,
                    session_id=state.session_id,
                    payload=payload,
                )
            )

        try:
            self._set_status(state.turn_id, TurnStatus.streaming)
            started.set()
            outcome = await self._pipeline.run(message, state, token, emit)
            token.raise_if_cancelled()
            self._outcomes[state.turn_id] = outcome
            # This synchronous state transition is the success commit point.
            # Public cancel/shutdown paths wait for, rather than cancel, the
            # remaining observers once the turn is terminal.
            completed = self._set_status(state.turn_id, TurnStatus.completed)
            await self._notify_turn_completed(message, completed, outcome)
            await self._publish(
                PipelineEvent(
                    type="assistant.completed",
                    turn_id=state.turn_id,
                    session_id=state.session_id,
                    payload={
                        "state": completed.model_dump(mode="json"),
                        "metrics": outcome.metrics.model_dump(mode="json"),
                    },
                )
            )
            log_event(
                self._logger,
                logging.INFO,
                "turn.completed",
                turn_id=state.turn_id,
                session_id=state.session_id,
                **outcome.metrics.model_dump(mode="json"),
            )
        except asyncio.CancelledError:
            self._set_status(state.turn_id, TurnStatus.cancelled)
            await self._publish_state_event(state.turn_id, "turn.cancelled")
            log_event(
                self._logger,
                logging.INFO,
                "turn.cancelled",
                turn_id=state.turn_id,
                session_id=state.session_id,
            )
            raise
        except Exception as exc:
            error_code = _safe_error_code(exc)
            self._set_status(state.turn_id, TurnStatus.failed, error_code=error_code)
            await self._publish_state_event(state.turn_id, "turn.failed")
            log_event(
                self._logger,
                logging.ERROR,
                "turn.failed",
                turn_id=state.turn_id,
                session_id=state.session_id,
                error_code=error_code,
            )
        finally:
            started.set()
            await self._end_user_priority(state.turn_id)
            self._tokens.pop(state.turn_id, None)
            self._tasks.pop(state.turn_id, None)
            if self._session_turn.get(state.session_id) == state.turn_id:
                self._session_turn.pop(state.session_id, None)

    async def cancel(
        self,
        *,
        session_id: str = "local_session",
        turn_id: str | None = None,
        reason: str = "user_interrupt",
    ) -> TurnState | None:
        async with self._coordination_lock:
            return await self._cancel_locked(
                session_id=session_id,
                turn_id=turn_id,
                reason=reason,
            )

    async def _cancel_locked(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        reason: str,
    ) -> TurnState | None:
        target_id = turn_id or self._session_turn.get(session_id)
        if target_id is None:
            return None
        state = self._states.get(target_id)
        if state is None:
            return state
        if state.status in {TurnStatus.cancelled, TurnStatus.completed, TurnStatus.failed}:
            task = self._tasks.get(target_id)
            if task is not None and task is not asyncio.current_task() and not task.done():
                await asyncio.gather(task, return_exceptions=True)
            return self._states.get(target_id)

        token = self._tokens.get(target_id)
        if token is not None:
            token.cancel()
        task = self._tasks.get(target_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif self._pipeline is None:
            state = self._set_status(target_id, TurnStatus.cancelled)
            await self._publish_state_event(target_id, "turn.cancelled")
        log_event(
            self._logger,
            logging.INFO,
            "turn.cancel.requested",
            turn_id=target_id,
            session_id=state.session_id if state else session_id,
            reason=reason,
        )
        return self._states.get(target_id)

    def subscribe(self, session_id: str) -> asyncio.Queue[PipelineEvent]:
        # Local single-user clients must never backpressure cancellation or shutdown.
        queue: asyncio.Queue[PipelineEvent] = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[PipelineEvent]) -> None:
        subscribers = self._subscribers.get(session_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(session_id, None)

    async def _publish(self, event: PipelineEvent) -> None:
        for sink in self._event_sinks:
            try:
                sink.publish(event)
            except Exception:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "turn.event_sink_failed",
                    sink=type(sink).__name__,
                    event_type=event.type,
                    turn_id=event.turn_id,
                )
        queues = {
            *self._subscribers.get(event.session_id, ()),
            *self._subscribers.get("*", ()),
        }
        for queue in tuple(queues):
            await queue.put(event)

    async def _publish_state_event(self, turn_id: str, event_type: str) -> None:
        state = self._states[turn_id]
        await self._publish(
            PipelineEvent(
                type=event_type,
                turn_id=turn_id,
                session_id=state.session_id,
                payload=state.model_dump(mode="json"),
            )
        )

    def _set_status(
        self,
        turn_id: str,
        status: TurnStatus,
        *,
        error_code: str | None = None,
    ) -> TurnState:
        state = self._states[turn_id].model_copy(
            update={"status": status, "updated_at": utc_now(), "error_code": error_code}
        )
        self._states[turn_id] = state
        return state

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_turns": sorted(self._tasks),
            "turns": {
                turn_id: state.model_dump(mode="json") for turn_id, state in self._states.items()
            },
            "metrics": {
                turn_id: outcome.metrics.model_dump(mode="json")
                for turn_id, outcome in self._outcomes.items()
            },
            "outcomes": {
                turn_id: {
                    "text_length": len(outcome.full_text),
                    "segment_count": len(outcome.segments),
                }
                for turn_id, outcome in self._outcomes.items()
            },
            "subscriber_count": sum(len(group) for group in self._subscribers.values()),
        }

    async def shutdown(self) -> None:
        async with self._coordination_lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(self._tasks.items())
            for turn_id, task in tasks:
                state = self._states.get(turn_id)
                if state is None or state.status not in {
                    TurnStatus.cancelled,
                    TurnStatus.completed,
                    TurnStatus.failed,
                }:
                    token = self._tokens.get(turn_id)
                    if token is not None:
                        token.cancel()
                    task.cancel()
        if tasks:
            await asyncio.gather(*(task for _turn_id, task in tasks), return_exceptions=True)
        if self._pipeline is not None:
            await self._pipeline.close()
        if self._event_sinks:
            await asyncio.gather(
                *(sink.close() for sink in self._event_sinks),
                return_exceptions=True,
            )

    async def _begin_user_priority(self, turn_id: str) -> None:
        if self._priority_controller is not None:
            await self._priority_controller.begin_user_turn(turn_id)

    async def _end_user_priority(self, turn_id: str) -> None:
        if self._priority_controller is not None:
            await self._priority_controller.end_user_turn(turn_id)

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


def _safe_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, Enum) and isinstance(code.value, str):
        return code.value
    if isinstance(code, str) and code:
        return code
    return "pipeline_failed"
