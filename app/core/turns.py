"""Globally arbitrated explicit and proactive dialogue lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, cast

from app.config.logging import log_event
from app.core.cancellation import CancellationToken
from app.core.contracts import TurnEventSink, TurnPriorityController
from app.schemas import (
    InputMode,
    PipelineEvent,
    ProactiveIntent,
    TurnOutcome,
    TurnState,
    TurnStatus,
    UserMessage,
    utc_now,
)

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]
_TERMINAL = {TurnStatus.cancelled, TurnStatus.completed, TurnStatus.failed}


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


@dataclass(slots=True)
class _ActiveTurn:
    turn_id: str
    session_id: str
    origin: str
    token: CancellationToken | None = None
    task: asyncio.Task[Any] | None = None
    cancellation_started: bool = False


class TurnService:
    """Serialize shared output and make every cancellation a one-shot operation."""

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
        self._observers = tuple(observers)
        self._event_sinks = tuple(event_sinks)
        self._priority_controller = priority_controller
        self._states: dict[str, TurnState] = {}
        self._outcomes: dict[str, TurnOutcome] = {}
        self._session_turn: dict[str, str] = {}
        self._active: _ActiveTurn | None = None
        self._pipeline_tasks: set[asyncio.Task[Any]] = set()
        self._post_commit_tasks: set[asyncio.Task[Any]] = set()
        self._accept_depths: dict[asyncio.Task[Any], int] = {}
        self._subscribers: dict[str, set[asyncio.Queue[PipelineEvent]]] = {}
        self._closed = False
        self._accept_lock = asyncio.Lock()
        self._coordination_lock = asyncio.Lock()
        self._output_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_started = asyncio.Event()

    async def accept(self, message: UserMessage) -> TurnState:
        """Persist an explicit input boundary before optionally starting generation."""

        owner = asyncio.current_task()
        assert owner is not None
        self._track_accept(owner)
        state = TurnState(
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
        )
        priority_owned = False
        registered = False
        pipeline_started = False
        try:
            # Only preemption and registration are serialized. Persistence observers
            # run outside this lock so another explicit input can always take priority.
            async with self._accept_lock:
                priority_owned = True
                await self._begin_user_priority(state.turn_id)
                old_task, immediate = await self._signal_active_cancellation()
                if old_task is not None:
                    await self._join_task(old_task)
                if immediate is not None:
                    cancelled, event_type = immediate
                    await self._publish_state(cancelled, event_type)

                async with self._coordination_lock:
                    if self._closed:
                        raise RuntimeError("TurnService 已关闭")
                    self._states[state.turn_id] = state
                    self._session_turn[state.session_id] = state.turn_id
                    self._active = _ActiveTurn(
                        turn_id=state.turn_id,
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
                    await self._publish(
                        PipelineEvent(
                            type="turn.accepted",
                            turn_id=state.turn_id,
                            session_id=state.session_id,
                            payload=state.model_dump(mode="json"),
                        )
                    )

            # This is intentionally outside both control locks. A message accepted
            # before cancellation remains eligible for seven-day history persistence.
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
                            self._run_user(message, state, token, started),
                            name=f"dialogue-{state.turn_id}",
                        )
                        active.token = token
                        active.task = task
                        self._track_pipeline_task(task)
                        # Holding the control lock prevents cancel/shutdown from
                        # cancelling the task before its protected finally is entered.
                        await started.wait()
                        priority_owned = False
                        pipeline_started = True
            if not pipeline_started:
                async with self._coordination_lock:
                    self._clear_active_locked(state.turn_id)

            # Keep the HTTP acceptance contract stable, while returning a terminal
            # registry state if cancellation happened during the acceptance observer.
            return state if pipeline_started else self._states[state.turn_id]
        except BaseException:
            if registered:
                cancel_task, immediate = await self._cancel_registered_turn(state.turn_id)
                if cancel_task is not None:
                    await self._join_task(cancel_task)
                if immediate is not None:
                    cancelled, event_type = immediate
                    await self._publish_state(cancelled, event_type)
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
            async with self._output_lock:
                token.raise_if_cancelled()
                outcome = await self._pipeline.run(message, state, token, emit)
            token.raise_if_cancelled()
            self._outcomes[state.turn_id] = outcome
            completed = self._set_status(state.turn_id, TurnStatus.completed)
            # Terminal commit and ownership release are synchronous: a replacement
            # can never self-join through a completion observer.
            self._clear_active_locked(state.turn_id)
            finalizer = asyncio.create_task(
                self._finalize_completed(message, completed, outcome),
                name=f"turn-post-commit-{state.turn_id}",
            )
            self._track_post_commit_task(finalizer)
        except asyncio.CancelledError:
            cancelled, changed = self._set_terminal_if_open(state.turn_id, TurnStatus.cancelled)
            self._clear_active_locked(state.turn_id)
            if changed:
                await self._publish_state(cancelled, "turn.cancelled")
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
                await self._publish_state(failed, "turn.failed")
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
    ) -> None:
        await self._notify_turn_completed(message, completed, outcome)
        await self._publish(
            PipelineEvent(
                type="assistant.completed",
                turn_id=completed.turn_id,
                session_id=completed.session_id,
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
            turn_id=completed.turn_id,
            session_id=completed.session_id,
            **outcome.metrics.model_dump(mode="json"),
        )

    async def run_proactive(
        self,
        intent: ProactiveIntent,
        token: CancellationToken,
    ) -> None:
        """Run an internal intent without constructing a user or memory source."""

        if self._pipeline is None:
            return
        task = asyncio.current_task()
        assert task is not None
        state = TurnState(
            session_id="local_session",
            source_message_id=intent.intent_id,
            input_mode=InputMode.text,
        )

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

        async with self._coordination_lock:
            if self._closed or self._active is not None:
                token.cancel()
                token.raise_if_cancelled()
            self._states[state.turn_id] = state
            self._session_turn[state.session_id] = state.turn_id
            self._active = _ActiveTurn(
                turn_id=state.turn_id,
                session_id=state.session_id,
                origin="proactive",
                token=token,
                task=task,
            )
            self._track_pipeline_task(task)
            await self._publish(
                PipelineEvent(
                    type="proactive.accepted",
                    turn_id=state.turn_id,
                    session_id=state.session_id,
                    payload={
                        "intent_id": intent.intent_id,
                        "trigger_type": intent.trigger_type,
                        "voice_allowed": intent.voice_allowed,
                    },
                )
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
            self._outcomes[state.turn_id] = outcome
            completed = self._set_status(state.turn_id, TurnStatus.completed)
            self._clear_active_locked(state.turn_id)
            await self._publish_state(completed, "proactive.completed")
        except asyncio.CancelledError:
            cancelled, changed = self._set_terminal_if_open(state.turn_id, TurnStatus.cancelled)
            self._clear_active_locked(state.turn_id)
            if changed:
                await self._publish_state(cancelled, "proactive.cancelled")
            raise
        except Exception as exc:
            failed, changed = self._set_terminal_if_open(
                state.turn_id,
                TurnStatus.failed,
                error_code=_safe_error_code(exc),
            )
            self._clear_active_locked(state.turn_id)
            if changed:
                await self._publish_state(failed, "proactive.failed")
        finally:
            self._clear_active_locked(state.turn_id)

    async def cancel(
        self,
        *,
        session_id: str = "local_session",
        turn_id: str | None = None,
        reason: str = "user_interrupt",
    ) -> TurnState | None:
        target_id = turn_id or self._session_turn.get(session_id)
        if target_id is None:
            return None
        task, immediate = await self._cancel_registered_turn(target_id)
        if immediate is not None:
            cancelled, event_type = immediate
            await self._publish_state(cancelled, event_type)
        if task is not None:
            await self._join_task(task)
        result = self._states.get(target_id)
        log_event(
            self._logger,
            logging.INFO,
            "turn.cancel.requested",
            turn_id=target_id,
            session_id=result.session_id if result else session_id,
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
            if state is None or state.status in _TERMINAL:
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
        if state.status in _TERMINAL:
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
            # ProactiveLifecycle may already have atomically signalled this same
            # token/task. Avoid a second Task.cancel() that could interrupt cleanup.
            already_signalled = token is not None and token.cancelled
            if token is not None:
                token.cancel()
            if not already_signalled and task is not asyncio.current_task():
                task.cancel()
        return task, None

    def subscribe(self, session_id: str) -> asyncio.Queue[PipelineEvent]:
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

    async def _publish_state(self, state: TurnState, event_type: str) -> None:
        await self._publish(
            PipelineEvent(
                type=event_type,
                turn_id=state.turn_id,
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

    def _set_terminal_if_open(
        self,
        turn_id: str,
        status: TurnStatus,
        *,
        error_code: str | None = None,
    ) -> tuple[TurnState, bool]:
        state = self._states[turn_id]
        if state.status in _TERMINAL:
            return state, False
        return self._set_status(turn_id, status, error_code=error_code), True

    def _clear_active_locked(self, turn_id: str) -> None:
        active = self._active
        if active is not None and active.turn_id == turn_id:
            self._active = None
        state = self._states.get(turn_id)
        if state is not None and self._session_turn.get(state.session_id) == turn_id:
            self._session_turn.pop(state.session_id, None)

    def snapshot(self) -> dict[str, Any]:
        active = self._active
        return {
            "active_turns": [active.turn_id] if active is not None else [],
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
                    **(
                        {"origin": outcome.metadata["origin"]}
                        if "origin" in outcome.metadata
                        else {}
                    ),
                }
                for turn_id, outcome in self._outcomes.items()
            },
            "subscriber_count": sum(len(group) for group in self._subscribers.values()),
        }

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(
                    self._shutdown_impl(), name="turn-service-shutdown"
                )
            task = self._shutdown_task
        current = asyncio.current_task()
        # An observer owned by this service must not await a shutdown task that is
        # intentionally draining that observer. It initiates shutdown and returns.
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
            await self._publish_state(state, event_type)

        await self._drain_tasks(tuple(self._accept_depths))
        await self._drain_tasks(tuple(self._pipeline_tasks))
        await self._drain_tasks(tuple(self._post_commit_tasks))

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
        return code.value
    if isinstance(code, str) and code:
        return code
    return "pipeline_failed"
