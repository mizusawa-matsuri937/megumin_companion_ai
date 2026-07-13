"""Non-blocking translation from dialogue events to the VTS action queue."""

from __future__ import annotations

from typing import Protocol

from app.clients.vts.bridge import VTSBridgeSnapshot
from app.schemas import PipelineEvent


class ExpressionBridge(Protocol):
    def start(self) -> None: ...

    def enqueue_expression(self, expression: str, *, turn_id: str | None = None) -> bool: ...

    def snapshot(self) -> VTSBridgeSnapshot: ...

    async def close(self) -> None: ...


class VTSTurnEventSink:
    """Queue expression metadata only; raw dialogue text never enters VTS."""

    def __init__(self, bridge: ExpressionBridge) -> None:
        self._bridge = bridge
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("VTS event sink 已关闭")
        self._bridge.start()

    def publish(self, event: PipelineEvent) -> bool:
        if self._closed:
            return False
        if event.type != "assistant.segment":
            return True
        if event.payload.get("expression_update") is False:
            return True
        expression = event.payload.get("live2d_expression")
        if not isinstance(expression, str) or not expression.strip():
            return False
        return self._bridge.enqueue_expression(expression, turn_id=event.turn_id)

    def snapshot(self) -> VTSBridgeSnapshot:
        return self._bridge.snapshot()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._bridge.close()
