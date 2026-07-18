"""Dialogue-to-VTS translation remains metadata-only and generation-aware."""

import asyncio

import pytest
from app.clients.vts import VTSBridgeSnapshot, VTSBridgeState, VTSTurnEventSink
from app.schemas import PipelineEvent


class FakeBridge:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.generation = 0
        self.turn_id: str | None = None
        self.expressions: list[tuple[str, str, int]] = []
        self.cancellations: list[tuple[str, int]] = []

    def start(self) -> None:
        self.started = True

    def begin_turn(self, turn_id: str) -> int | None:
        self.generation += 1
        self.turn_id = turn_id
        return self.generation

    def cancel_turn(self, turn_id: str, generation: int) -> bool:
        self.cancellations.append((turn_id, generation))
        if turn_id != self.turn_id or generation != self.generation:
            return False
        self.generation += 1
        self.turn_id = None
        return True

    def enqueue_expression(self, expression: str, *, turn_id: str, generation: int) -> bool:
        self.expressions.append((expression, turn_id, generation))
        return expression != "drop"

    def snapshot(self) -> VTSBridgeSnapshot:
        return VTSBridgeSnapshot(
            state=VTSBridgeState.ready,
            queue_size=0,
            dropped_actions=0,
            processed_actions=len(self.expressions),
            reconnect_count=0,
            missing_expression_count=0,
            generation=self.generation,
        )

    async def close(self) -> None:
        self.closed = True


def event(
    event_type: str, payload: dict[str, object], *, turn_id: str = "turn_test"
) -> PipelineEvent:
    return PipelineEvent(
        seq=1,
        type=event_type,
        turn_id=turn_id,
        session_id="session_test",
        payload=payload,
    )


def test_sink_forwards_only_current_generation_expression_metadata() -> None:
    async def scenario() -> None:
        bridge = FakeBridge()
        sink = VTSTurnEventSink(bridge)
        sink.start()

        assert sink.publish(event("turn.accepted", {"state": {"private": "ignored"}}))
        assert sink.publish(event("assistant.delta", {"delta": "private dialogue"}))
        assert sink.publish(
            event(
                "assistant.segment",
                {"live2d_expression": "ignored", "expression_update": False},
            )
        )
        assert not sink.publish(event("assistant.segment", {"text": "private dialogue"}))
        assert sink.publish(
            event(
                "assistant.segment",
                {"text": "private dialogue", "live2d_expression": "happy"},
            )
        )
        assert not sink.publish(
            event("assistant.segment", {"live2d_expression": "happy"}, turn_id="turn_old")
        )
        assert not sink.publish(event("assistant.segment", {"live2d_expression": "drop"}))
        assert bridge.expressions == [
            ("happy", "turn_test", 1),
            ("drop", "turn_test", 1),
        ]

        assert sink.publish(event("turn.cancelled", {}))
        assert bridge.cancellations == [("turn_test", 1)]
        assert not sink.publish(event("assistant.segment", {"live2d_expression": "happy"}))

        assert sink.publish(event("turn.accepted", {}, turn_id="turn_new"))
        assert sink.publish(
            event("assistant.segment", {"live2d_expression": "happy"}, turn_id="turn_new")
        )
        assert bridge.expressions[-1] == ("happy", "turn_new", 3)

        await sink.close()
        await sink.close()
        assert bridge.closed
        assert not sink.publish(event("assistant.segment", {"live2d_expression": "happy"}))
        with pytest.raises(RuntimeError, match="已关闭"):
            sink.start()

    asyncio.run(scenario())
