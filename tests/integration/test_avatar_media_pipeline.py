"""Whole-turn Avatar sink plus parent MediaWorker progress routing integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.avatar import (
    AvatarHealthSnapshot,
    AvatarRuntimeState,
    AvatarTurnEventSink,
    AvatarTurnPlan,
)
from app.clients.llm import MockLLMProvider
from app.clients.tts import MockTTSProvider
from app.core import TurnService
from app.media import MediaWorkerAudioPlayer, MouthEnvelopeSample
from app.schemas import DialogueSegment, PipelineEvent, UserMessage
from app.workers import ResourceReference, WorkerJobProgress


class _ProgressSupervisor:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.playback_jobs: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def run_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        resources: Sequence[ResourceReference] = (),
        hard_deadline_seconds: float | None = None,
        progress_callback: Callable[[WorkerJobProgress], None] | None = None,
    ) -> dict[str, Any]:
        del hard_deadline_seconds
        if job_kind == "media.release":
            return {"status": "released"}
        assert job_kind == "media.play"
        assert len(resources) == 1
        assert progress_callback is not None
        self.playback_jobs.append(job_id)
        progress_callback(WorkerJobProgress(job_id, "mouth_envelope", 1, 0.25))
        progress_callback(WorkerJobProgress(job_id, "mouth_envelope", 2, 0.75))
        return {"status": "played", "notice_code": ""}

    async def cancel_job(self, job_id: str, *, deadline_at: float | None = None) -> None:
        del job_id, deadline_at

    async def stop(self) -> None:
        self.stopped = True


class _RecordingAvatarRuntime:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.turn_id: str | None = None
        self.generation = 0
        self.plans: list[AvatarTurnPlan] = []
        self.armed: list[str] = []
        self.samples: list[MouthEnvelopeSample] = []
        self.visual_fallbacks = 0
        self.completed: list[str] = []
        self.cancelled: list[str] = []

    def start(self) -> None:
        self.started = True

    def begin_turn(self, turn_id: str) -> int:
        self.generation += 1
        self.turn_id = turn_id
        return self.generation

    def set_turn_plan(self, plan: AvatarTurnPlan, *, generation: int) -> bool:
        if generation != self.generation or plan.turn_id != self.turn_id:
            return False
        self.plans.append(plan)
        return True

    def arm_playback(self, turn_id: str, *, generation: int) -> bool:
        if generation != self.generation or turn_id != self.turn_id:
            return False
        self.armed.append(turn_id)
        return True

    def offer_mouth_envelope(self, sample: MouthEnvelopeSample) -> bool:
        if sample.turn_id != self.turn_id:
            return False
        self.samples.append(sample)
        return True

    def visual_fallback(self, turn_id: str, *, generation: int) -> bool:
        if generation != self.generation or turn_id != self.turn_id:
            return False
        self.visual_fallbacks += 1
        return True

    def complete_turn(self, turn_id: str, *, generation: int) -> bool:
        if generation != self.generation or turn_id != self.turn_id:
            return False
        self.completed.append(turn_id)
        return True

    def cancel_turn(self, turn_id: str, *, generation: int) -> bool:
        if generation != self.generation or turn_id != self.turn_id:
            return False
        self.cancelled.append(turn_id)
        return True

    def trigger_red_eye(self) -> bool:
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


class _FailingAvatarRuntime(_RecordingAvatarRuntime):
    def begin_turn(self, turn_id: str) -> int:
        del turn_id
        raise RuntimeError("avatar_runtime_failed")


def _happy(segment: DialogueSegment) -> DialogueSegment:
    return segment.model_copy(update={"emotion": "happy"})


def test_turn_service_routes_each_actual_playback_job_envelope_to_one_frozen_plan(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[_RecordingAvatarRuntime, _ProgressSupervisor, str]:
        runtime = _RecordingAvatarRuntime()
        supervisor = _ProgressSupervisor()
        player = MediaWorkerAudioPlayer(
            roots={"audio_temp": tmp_path},
            selected_device_id=None,
            supervisor=supervisor,
            mouth_envelope_listener=runtime.offer_mouth_envelope,
        )
        from app.pipelines import DialoguePipeline

        pipeline = DialoguePipeline(
            MockLLMProvider(
                deltas=["第一段合成测试已经完成。", "第二段合成测试也已经完成。"],
                token_delay_seconds=0,
            ),
            MockTTSProvider(tmp_path, duration_ms=1, synthesis_delay_seconds=0),
            player,
            segment_decorator=_happy,
            tts_connect_timeout_ms=80,
            tts_first_byte_timeout_ms=80,
            tts_total_timeout_ms=300,
            tts_cancellation_timeout_ms=50,
        )
        sink = AvatarTurnEventSink(runtime)
        sink.start()
        logger = logging.getLogger("test.avatar_media_pipeline")
        logger.addHandler(logging.NullHandler())
        service = TurnService(logger, pipeline, event_sinks=(sink,))
        subscription = await service.subscribe("local_session")
        state = await service.accept(UserMessage(text="运行合成全链路测试"))
        try:
            while True:
                event = await asyncio.wait_for(subscription.get(), timeout=2)
                assert isinstance(event, PipelineEvent)
                subscription.task_done()
                if event.type == "assistant.completed" and event.turn_id == state.turn_id:
                    break
        finally:
            await service.shutdown()
        return runtime, supervisor, state.turn_id

    runtime, supervisor, turn_id = asyncio.run(scenario())

    assert runtime.started and runtime.closed
    assert supervisor.started and supervisor.stopped
    assert len(runtime.plans) == 1
    assert runtime.plans[0].turn_id == turn_id
    assert runtime.plans[0].emotion.value == "happy"
    assert len(supervisor.playback_jobs) == 2
    assert runtime.armed == [turn_id, turn_id]
    assert runtime.visual_fallbacks == 1
    assert runtime.completed == [turn_id]
    assert not runtime.cancelled
    assert len(runtime.samples) == 6
    assert {sample.turn_id for sample in runtime.samples} == {turn_id}
    assert {sample.playback_job_id for sample in runtime.samples} == set(supervisor.playback_jobs)
    for job_id in supervisor.playback_jobs:
        samples = [item for item in runtime.samples if item.playback_job_id == job_id]
        assert [item.value for item in samples] == [0.25, 0.75, 0.0]
        assert [item.sequence for item in samples] == [1, 2, 3]
        assert samples[-1].terminal


def test_avatar_runtime_failure_does_not_block_text_or_media_worker_playback(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[
        _FailingAvatarRuntime,
        _ProgressSupervisor,
        list[str],
    ]:
        runtime = _FailingAvatarRuntime()
        supervisor = _ProgressSupervisor()
        player = MediaWorkerAudioPlayer(
            roots={"audio_temp": tmp_path},
            selected_device_id=None,
            supervisor=supervisor,
            mouth_envelope_listener=runtime.offer_mouth_envelope,
        )
        from app.pipelines import DialoguePipeline

        pipeline = DialoguePipeline(
            MockLLMProvider(
                deltas=["Avatar 故障时文字与音频仍应完成。"],
                token_delay_seconds=0,
            ),
            MockTTSProvider(tmp_path, duration_ms=1, synthesis_delay_seconds=0),
            player,
            tts_connect_timeout_ms=80,
            tts_first_byte_timeout_ms=80,
            tts_total_timeout_ms=300,
            tts_cancellation_timeout_ms=50,
        )
        sink = AvatarTurnEventSink(runtime)
        sink.start()
        logger = logging.getLogger("test.avatar_failure_isolation")
        logger.addHandler(logging.NullHandler())
        service = TurnService(logger, pipeline, event_sinks=(sink,))
        subscription = await service.subscribe("local_session")
        state = await service.accept(UserMessage(text="运行 Avatar 故障隔离测试"))
        event_types: list[str] = []
        try:
            while True:
                event = await asyncio.wait_for(subscription.get(), timeout=2)
                assert isinstance(event, PipelineEvent)
                subscription.task_done()
                if event.turn_id == state.turn_id:
                    event_types.append(event.type)
                if event.type == "assistant.completed" and event.turn_id == state.turn_id:
                    break
        finally:
            await service.shutdown()
        return runtime, supervisor, event_types

    runtime, supervisor, event_types = asyncio.run(scenario())

    assert runtime.started and runtime.closed
    assert supervisor.started and supervisor.stopped
    assert len(supervisor.playback_jobs) == 1
    assert "assistant.delta" in event_types
    assert "playback.started" in event_types
    assert "playback.finished" in event_types
    assert event_types[-1] == "assistant.completed"
    assert "turn.failed" not in event_types
