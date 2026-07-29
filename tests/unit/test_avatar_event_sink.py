"""Whole-turn W28 Avatar event translation tests."""

from __future__ import annotations

import asyncio

from app.avatar import (
    AvatarHealthSnapshot,
    AvatarRuntimeState,
    AvatarTurnPlan,
    AvatarTurnEventSink,
)
from app.schemas import PipelineEvent


class _FakeRuntime:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.generation = 0
        self.turn_id: str | None = None
        self.plans: list[AvatarTurnPlan] = []
        self.armed: list[tuple[str, int]] = []
        self.fallbacks: list[tuple[str, int]] = []
        self.completed: list[tuple[str, int]] = []
        self.cancelled: list[tuple[str, int]] = []
        self.red_eye_trigger_count = 0

    def start(self) -> None:
        self.started = True

    def begin_turn(self, turn_id: str) -> int | None:
        self.generation += 1
        self.turn_id = turn_id
        return self.generation

    def set_turn_plan(self, plan: AvatarTurnPlan, *, generation: int) -> bool:
        if generation != self.generation:
            return False
        self.plans.append(plan)
        return True

    def arm_playback(self, turn_id: str, *, generation: int) -> bool:
        self.armed.append((turn_id, generation))
        return generation == self.generation

    def visual_fallback(self, turn_id: str, *, generation: int) -> bool:
        self.fallbacks.append((turn_id, generation))
        return generation == self.generation

    def complete_turn(self, turn_id: str, *, generation: int) -> bool:
        self.completed.append((turn_id, generation))
        return generation == self.generation

    def cancel_turn(self, turn_id: str, *, generation: int) -> bool:
        self.cancelled.append((turn_id, generation))
        return generation == self.generation

    def trigger_red_eye(self) -> bool:
        self.red_eye_trigger_count += 1
        return True

    def snapshot(self) -> AvatarHealthSnapshot:
        return AvatarHealthSnapshot(
            state=AvatarRuntimeState.ready,
            parameter_control_available=True,
            lip_sync_available=True,
            body_motion_available=True,
            automatic_red_eye_available=True,
            sent_frames=0,
            coalesced_frames=0,
            dropped_actions=0,
            reconnect_count=0,
        )

    async def close(self) -> None:
        self.closed = True


def _event(
    event_type: str,
    payload: dict[str, object] | None = None,
    *,
    turn_id: str = "turn_test",
) -> PipelineEvent:
    return PipelineEvent(
        seq=1,
        type=event_type,
        turn_id=turn_id,
        session_id="session_test",
        payload=payload or {},
    )


def test_sink_freezes_first_segment_and_uses_actual_or_fallback_playback_once() -> None:
    async def scenario() -> None:
        runtime = _FakeRuntime()
        sink = AvatarTurnEventSink(runtime)
        sink.start()

        assert sink.publish(_event("turn.accepted"))
        assert sink.publish(_event("assistant.segment", {"emotion": "happy"}))
        assert sink.publish(_event("assistant.segment", {"emotion": "focused"}))
        assert len(runtime.plans) == 1
        assert runtime.plans[0].emotion.value == "happy"

        assert sink.publish(_event("playback.started", {"index": 0}))
        assert runtime.armed == [("turn_test", 1)]
        assert sink.publish(_event("playback.finished", {"index": 0}))
        assert runtime.fallbacks == [("turn_test", 1)]
        assert sink.publish(_event("playback.skipped", {"index": 1}))
        assert runtime.fallbacks == [("turn_test", 1)]

        assert sink.publish(_event("assistant.completed"))
        assert runtime.completed == [("turn_test", 1)]

        assert sink.publish(_event("turn.accepted", turn_id="turn_cancel"))
        assert sink.publish(
            _event(
                "assistant.segment",
                {"emotion": "focused"},
                turn_id="turn_cancel",
            )
        )
        assert sink.publish(_event("turn.cancelled", turn_id="turn_cancel"))
        assert runtime.cancelled == [("turn_cancel", 2)]

        await sink.close()
        await sink.close()
        assert runtime.closed
        assert not sink.publish(_event("turn.accepted", turn_id="late"))

    asyncio.run(scenario())
