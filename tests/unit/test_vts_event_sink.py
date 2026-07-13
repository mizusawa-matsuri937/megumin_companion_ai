"""Dialogue-to-VTS event translation stays metadata-only and non-blocking."""

import asyncio

import pytest
from app.clients.vts import VTSBridgeSnapshot, VTSBridgeState, VTSTurnEventSink
from app.schemas import PipelineEvent


class FakeBridge:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.expressions: list[tuple[str, str | None]] = []

    def start(self) -> None:
        self.started = True

    def enqueue_expression(self, expression: str, *, turn_id: str | None = None) -> bool:
        self.expressions.append((expression, turn_id))
        return expression != "drop"

    def snapshot(self) -> VTSBridgeSnapshot:
        return VTSBridgeSnapshot(
            state=VTSBridgeState.ready,
            queue_size=0,
            dropped_actions=0,
            processed_actions=len(self.expressions),
            reconnect_count=0,
            missing_expression_count=0,
        )

    async def close(self) -> None:
        self.closed = True


def event(event_type: str, payload: dict[str, object]) -> PipelineEvent:
    return PipelineEvent(
        type=event_type,
        turn_id="turn_test",
        session_id="session_test",
        payload=payload,
    )


def test_sink_forwards_only_expression_metadata() -> None:
    async def scenario() -> None:
        bridge = FakeBridge()
        sink = VTSTurnEventSink(bridge)
        sink.start()

        assert sink.publish(event("assistant.delta", {"delta": "private dialogue"}))
        assert not sink.publish(event("assistant.segment", {"text": "private dialogue"}))
        assert sink.publish(
            event(
                "assistant.segment",
                {"text": "private dialogue", "live2d_expression": "happy"},
            )
        )
        assert not sink.publish(event("assistant.segment", {"live2d_expression": "drop"}))
        assert bridge.expressions == [("happy", "turn_test"), ("drop", "turn_test")]
        assert sink.snapshot().processed_actions == 2

        await sink.close()
        await sink.close()
        assert bridge.closed
        assert not sink.publish(event("assistant.segment", {"live2d_expression": "happy"}))
        with pytest.raises(RuntimeError, match="已关闭"):
            sink.start()

    asyncio.run(scenario())
