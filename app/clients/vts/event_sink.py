"""Non-blocking translation from dialogue events to the VTS action queue."""

from __future__ import annotations

from typing import Protocol

from app.clients.vts.bridge import VTSBridgeSnapshot, VTSBridgeState
from app.schemas import PipelineEvent


class ExpressionBridge(Protocol):
    def start(self) -> None: ...

    def begin_turn(self, turn_id: str) -> int | None: ...

    def cancel_turn(self, turn_id: str, generation: int) -> bool: ...

    def enqueue_expression(self, expression: str, *, turn_id: str, generation: int) -> bool: ...

    def snapshot(self) -> VTSBridgeSnapshot: ...

    async def close(self) -> None: ...


class VTSTurnEventSink:
    """Queue expression metadata only; raw dialogue text never enters VTS."""

    def __init__(self, bridge: ExpressionBridge) -> None:
        self._bridge = bridge
        self._closed = False
        self._turn_id: str | None = None
        self._generation: int | None = None

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("VTS event sink 已关闭")
        self._bridge.start()

    def publish(self, event: PipelineEvent) -> bool:
        if self._closed:
            return False
        if self._bridge.snapshot().state is VTSBridgeState.disabled:
            return True
        if event.type in {"turn.accepted", "proactive.accepted"}:
            if event.turn_id is None:
                return False
            generation = self._bridge.begin_turn(event.turn_id)
            if generation is None:
                return False
            self._turn_id = event.turn_id
            self._generation = generation
            return True
        if event.type in {"turn.cancelled", "turn.failed"}:
            if event.turn_id is None or event.turn_id != self._turn_id or self._generation is None:
                return True
            cancelled = self._bridge.cancel_turn(event.turn_id, self._generation)
            if cancelled:
                self._turn_id = None
                self._generation = None
            return cancelled
        if event.type != "assistant.segment":
            return True
        if event.payload.get("expression_update") is False:
            return True
        expression = event.payload.get("live2d_expression")
        if not isinstance(expression, str) or not expression.strip():
            return False
        if event.turn_id is None or event.turn_id != self._turn_id or self._generation is None:
            return False
        return self._bridge.enqueue_expression(
            expression,
            turn_id=event.turn_id,
            generation=self._generation,
        )

    def snapshot(self) -> VTSBridgeSnapshot:
        return self._bridge.snapshot()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._turn_id = None
        self._generation = None
        await self._bridge.close()
