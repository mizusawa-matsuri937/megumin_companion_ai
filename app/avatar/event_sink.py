"""Whole-turn, body-free dialogue event adapter for the Avatar Runtime."""

from __future__ import annotations

from typing import Protocol

from app.avatar.mapper import map_avatar_turn_plan
from app.avatar.models import AvatarHealthSnapshot, AvatarTurnPlan
from app.emotion import EmotionLabel, FocusedVariant
from app.schemas import PipelineEvent


class AvatarEventRuntime(Protocol):
    def start(self) -> None: ...

    def begin_turn(self, turn_id: str) -> int | None: ...

    def set_turn_plan(self, plan: AvatarTurnPlan, *, generation: int) -> bool: ...

    def arm_playback(self, turn_id: str, *, generation: int) -> bool: ...

    def visual_fallback(self, turn_id: str, *, generation: int) -> bool: ...

    def complete_turn(self, turn_id: str, *, generation: int) -> bool: ...

    def cancel_turn(self, turn_id: str, *, generation: int) -> bool: ...

    def trigger_red_eye(self) -> bool: ...

    def snapshot(self) -> AvatarHealthSnapshot: ...

    async def close(self) -> None: ...


class AvatarTurnEventSink:
    """Freeze one plan and one body trigger per complete assistant turn."""

    def __init__(self, runtime: AvatarEventRuntime) -> None:
        self._runtime = runtime
        self._closed = False
        self._turn_id: str | None = None
        self._generation: int | None = None
        self._plan_frozen = False
        self._visual_requested = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Avatar event sink is closed")
        self._runtime.start()

    def publish(self, event: PipelineEvent) -> bool:
        if self._closed:
            return False
        if event.type in {"turn.accepted", "proactive.accepted"}:
            if event.turn_id is None:
                return False
            generation = self._runtime.begin_turn(event.turn_id)
            if generation is None:
                return False
            self._turn_id = event.turn_id
            self._generation = generation
            self._plan_frozen = False
            self._visual_requested = False
            return True
        if event.type in {
            "turn.cancelled",
            "turn.failed",
            "proactive.cancelled",
            "proactive.failed",
        }:
            return self._cancel(event)
        if not self._is_current(event):
            return event.type not in {
                "assistant.segment",
                "avatar.plan",
                "avatar.visual_fallback",
                "playback.started",
                "playback.finished",
                "playback.skipped",
                "assistant.completed",
                "avatar.red_eye",
            }
        assert self._turn_id is not None and self._generation is not None
        if event.type in {"assistant.segment", "avatar.plan"}:
            if self._plan_frozen:
                return True
            raw_emotion = event.payload.get("emotion", EmotionLabel.neutral.value)
            raw_variant = event.payload.get(
                "focused_variant",
                FocusedVariant.default.value,
            )
            try:
                emotion = EmotionLabel(raw_emotion)
                variant = FocusedVariant(raw_variant)
                plan = map_avatar_turn_plan(
                    self._turn_id,
                    emotion,
                    focused_variant=variant,
                )
            except (TypeError, ValueError):
                return False
            accepted = self._runtime.set_turn_plan(plan, generation=self._generation)
            self._plan_frozen = accepted
            return accepted
        if event.type == "playback.started":
            return self._runtime.arm_playback(
                self._turn_id,
                generation=self._generation,
            )
        if event.type in {"playback.finished", "playback.skipped"}:
            return self._request_visual_once()
        if event.type == "avatar.visual_fallback":
            return self._request_visual_once()
        if event.type == "avatar.red_eye":
            return event.payload == {"effect": "red_eye"} and self._runtime.trigger_red_eye()
        if event.type == "assistant.completed":
            if not self._request_visual_once():
                return False
            completed = self._runtime.complete_turn(
                self._turn_id,
                generation=self._generation,
            )
            if completed:
                self._clear()
            return completed
        return True

    def snapshot(self) -> AvatarHealthSnapshot:
        return self._runtime.snapshot()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._clear()
        await self._runtime.close()

    def _request_visual_once(self) -> bool:
        if self._visual_requested:
            return True
        assert self._turn_id is not None and self._generation is not None
        accepted = self._runtime.visual_fallback(
            self._turn_id,
            generation=self._generation,
        )
        self._visual_requested = accepted
        return accepted

    def _cancel(self, event: PipelineEvent) -> bool:
        if not self._is_current(event):
            return True
        assert self._turn_id is not None and self._generation is not None
        cancelled = self._runtime.cancel_turn(
            self._turn_id,
            generation=self._generation,
        )
        if cancelled:
            self._clear()
        return cancelled

    def _is_current(self, event: PipelineEvent) -> bool:
        return (
            event.turn_id is not None
            and event.turn_id == self._turn_id
            and self._generation is not None
        )

    def _clear(self) -> None:
        self._turn_id = None
        self._generation = None
        self._plan_frozen = False
        self._visual_requested = False
