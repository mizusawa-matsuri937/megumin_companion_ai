"""Single dialogue entry point shared by text and transcribed voice messages."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.config.logging import log_event
from app.core.cancellation import CancellationToken
from app.schemas import (
    PipelineEvent,
    TurnMetrics,
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
    ) -> TurnMetrics: ...

    async def close(self) -> None: ...


class TurnService:
    """Manage turn isolation, event delivery, cancellation, and shutdown cleanup."""

    def __init__(self, logger: logging.Logger, pipeline: TurnPipeline | None = None) -> None:
        self._logger = logger
        self._pipeline = pipeline
        self._states: dict[str, TurnState] = {}
        self._metrics: dict[str, TurnMetrics] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._session_turn: dict[str, str] = {}
        self._subscribers: dict[str, set[asyncio.Queue[PipelineEvent]]] = {}
        self._closed = False

    async def accept(self, message: UserMessage) -> TurnState:
        if self._closed:
            raise RuntimeError("TurnService 已关闭")

        # A new explicit input is a hard event barrier: the previous turn is fully
        # cancelled before the new accepted event can be published.
        await self.cancel(session_id=message.session_id, reason="superseded")
        state = TurnState(
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
        )
        self._states[state.turn_id] = state
        self._session_turn[message.session_id] = state.turn_id
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

        if self._pipeline is not None:
            token = CancellationToken(state.turn_id)
            self._tokens[state.turn_id] = token
            self._tasks[state.turn_id] = asyncio.create_task(
                self._run(message, state, token), name=f"dialogue-{state.turn_id}"
            )
        else:
            self._session_turn.pop(message.session_id, None)
        return state

    async def _run(self, message: UserMessage, state: TurnState, token: CancellationToken) -> None:
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

        self._set_status(state.turn_id, TurnStatus.streaming)
        try:
            metrics = await self._pipeline.run(message, state, token, emit)
            token.raise_if_cancelled()
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
        except Exception:
            self._set_status(state.turn_id, TurnStatus.failed, error_code="pipeline_failed")
            await self._publish_state_event(state.turn_id, "turn.failed")
            log_event(
                self._logger,
                logging.ERROR,
                "turn.failed",
                turn_id=state.turn_id,
                session_id=state.session_id,
                error_code="pipeline_failed",
            )
        else:
            self._metrics[state.turn_id] = metrics
            completed = self._set_status(state.turn_id, TurnStatus.completed)
            await self._publish(
                PipelineEvent(
                    type="assistant.completed",
                    turn_id=state.turn_id,
                    session_id=state.session_id,
                    payload={
                        "state": completed.model_dump(mode="json"),
                        "metrics": metrics.model_dump(mode="json"),
                    },
                )
            )
            log_event(
                self._logger,
                logging.INFO,
                "turn.completed",
                turn_id=state.turn_id,
                session_id=state.session_id,
                **metrics.model_dump(mode="json"),
            )
        finally:
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
        target_id = turn_id or self._session_turn.get(session_id)
        if target_id is None:
            return None
        state = self._states.get(target_id)
        if state is None or state.status in {
            TurnStatus.cancelled,
            TurnStatus.completed,
            TurnStatus.failed,
        }:
            return state

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
                turn_id: metrics.model_dump(mode="json")
                for turn_id, metrics in self._metrics.items()
            },
            "subscriber_count": sum(len(group) for group in self._subscribers.values()),
        }

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for token in self._tokens.values():
            token.cancel()
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._pipeline is not None:
            await self._pipeline.close()
