"""Mock streaming -> segmentation -> concurrent TTS -> ordered playback pipeline."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.clients.llm import LLMProvider
from app.clients.tts import TTSProvider
from app.core.cancellation import CancellationToken
from app.core.context import (
    ContextBuilder,
    DirectContextBuilder,
    DirectProactiveContextBuilder,
    ProactiveContextBuilder,
)
from app.pipelines.audio_player import AudioPlayer
from app.pipelines.segmenter import DialogueSegmenter
from app.schemas import (
    AudioResult,
    ChatRequest,
    DialogueSegment,
    ProactiveIntent,
    TTSJob,
    TurnMetrics,
    TurnOutcome,
    TurnState,
    UserMessage,
)

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]
SegmentDecorator = Callable[[DialogueSegment], DialogueSegment]


@dataclass(slots=True)
class _IndexedJob:
    index: int
    job: TTSJob


@dataclass(slots=True)
class _IndexedAudio:
    index: int
    result: AudioResult
    ready_at: float


class DialoguePipeline:
    """Own all queues for one turn so stale work cannot leak into a newer turn."""

    def __init__(
        self,
        llm: LLMProvider,
        tts: TTSProvider,
        audio_player: AudioPlayer,
        *,
        context_builder: ContextBuilder | None = None,
        proactive_context_builder: ProactiveContextBuilder | None = None,
        segment_decorator: SegmentDecorator | None = None,
        tts_worker_count: int = 2,
        segment_min_chars: int = 6,
        segment_max_chars: int = 42,
        segment_max_words: int = 25,
    ) -> None:
        if tts_worker_count < 1:
            raise ValueError("tts_worker_count 必须大于 0")
        self._llm = llm
        self._tts = tts
        self._audio_player = audio_player
        self._context_builder = context_builder or DirectContextBuilder()
        self._proactive_context_builder = (
            proactive_context_builder or DirectProactiveContextBuilder()
        )
        self._segment_decorator = segment_decorator
        self._tts_worker_count = tts_worker_count
        self._segment_min_chars = segment_min_chars
        self._segment_max_chars = segment_max_chars
        self._segment_max_words = segment_max_words
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    async def run(
        self,
        message: UserMessage,
        state: TurnState,
        token: CancellationToken,
        emit: EventEmitter,
    ) -> TurnOutcome:
        started = time.perf_counter()
        request = await self._context_builder.build(message)
        return await self._run_request(
            request,
            state,
            token,
            emit,
            started=started,
            audio_enabled=True,
        )

    async def run_proactive(
        self,
        intent: ProactiveIntent,
        state: TurnState,
        token: CancellationToken,
        emit: EventEmitter,
    ) -> TurnOutcome:
        started = time.perf_counter()
        request = await self._proactive_context_builder.build_proactive(intent)
        return await self._run_request(
            request,
            state,
            token,
            emit,
            started=started,
            audio_enabled=intent.voice_allowed,
        )

    async def _run_request(
        self,
        request: ChatRequest,
        state: TurnState,
        token: CancellationToken,
        emit: EventEmitter,
        *,
        started: float,
        audio_enabled: bool,
    ) -> TurnOutcome:
        metrics = TurnMetrics()
        full_text_parts: list[str] = []
        completed_segments: list[DialogueSegment] = []
        tts_queue: asyncio.Queue[_IndexedJob | None] = asyncio.Queue()
        audio_queue: asyncio.Queue[_IndexedAudio | None] = asyncio.Queue()
        cleanup: dict[str, AudioResult] = {}

        async def stream_to_tts() -> None:
            segmenter = DialogueSegmenter(
                state.turn_id,
                min_chars=self._segment_min_chars,
                max_chars=self._segment_max_chars,
                max_words=self._segment_max_words,
            )
            try:
                async for delta in self._llm.stream(request, token):
                    token.raise_if_cancelled()
                    full_text_parts.append(delta)
                    if metrics.llm_first_token_ms is None:
                        metrics.llm_first_token_ms = _elapsed_ms(started)
                    await emit("assistant.delta", {"delta": delta})
                    for segment in segmenter.feed(delta):
                        await self._queue_segment(
                            segment,
                            token,
                            metrics,
                            completed_segments,
                            started,
                            emit,
                            tts_queue if audio_enabled else None,
                        )
                token.raise_if_cancelled()
                for segment in segmenter.flush():
                    await self._queue_segment(
                        segment,
                        token,
                        metrics,
                        completed_segments,
                        started,
                        emit,
                        tts_queue if audio_enabled else None,
                    )
            finally:
                if audio_enabled:
                    for _ in range(self._tts_worker_count):
                        await tts_queue.put(None)

        async def tts_worker() -> None:
            try:
                while True:
                    indexed_job = await tts_queue.get()
                    try:
                        if indexed_job is None:
                            return
                        token.raise_if_cancelled()
                        synthesized_at = time.perf_counter()
                        try:
                            result = await asyncio.wait_for(
                                self._tts.synthesize(
                                    indexed_job.job,
                                    segment_index=indexed_job.index,
                                    token=token,
                                ),
                                timeout=indexed_job.job.timeout_ms / 1000,
                            )
                        except TimeoutError:
                            result = AudioResult(
                                job_id=indexed_job.job.job_id,
                                turn_id=indexed_job.job.turn_id,
                                segment_id=indexed_job.job.segment_id,
                                success=False,
                                error_code="tts_timeout",
                            )
                        ready_at = time.perf_counter()
                        metrics.tts_job_latency_ms.append(
                            max(0, round((ready_at - synthesized_at) * 1000))
                        )
                        if result.success:
                            cleanup[result.audio_id] = result
                            if metrics.tts_first_audio_ms is None:
                                metrics.tts_first_audio_ms = _elapsed_ms(started)
                        await emit(
                            "audio.ready",
                            {
                                "index": indexed_job.index,
                                "audio_id": result.audio_id,
                                "segment_id": result.segment_id,
                                "success": result.success,
                                "duration_ms": result.duration_ms,
                                "error_code": result.error_code,
                            },
                        )
                        await audio_queue.put(
                            _IndexedAudio(
                                index=indexed_job.index,
                                result=result,
                                ready_at=ready_at,
                            )
                        )
                    finally:
                        tts_queue.task_done()
            finally:
                await audio_queue.put(None)

        async def ordered_playback() -> None:
            pending: dict[int, _IndexedAudio] = {}
            next_index = 0
            finished_workers = 0
            while finished_workers < self._tts_worker_count:
                indexed_audio = await audio_queue.get()
                try:
                    if indexed_audio is None:
                        finished_workers += 1
                    else:
                        pending[indexed_audio.index] = indexed_audio
                    while next_index in pending:
                        token.raise_if_cancelled()
                        current = pending.pop(next_index)
                        result = current.result
                        if not result.success:
                            await emit(
                                "playback.skipped",
                                {"index": next_index, "error_code": result.error_code},
                            )
                            next_index += 1
                            continue
                        metrics.audio_queue_wait_ms.append(
                            max(0, round((time.perf_counter() - current.ready_at) * 1000))
                        )
                        if metrics.first_sentence_play_ms is None:
                            metrics.first_sentence_play_ms = _elapsed_ms(started)
                        await emit(
                            "playback.started",
                            {
                                "index": next_index,
                                "audio_id": result.audio_id,
                                "segment_id": result.segment_id,
                            },
                        )
                        try:
                            await self._audio_player.play(result, token)
                            token.raise_if_cancelled()
                            metrics.playback_count += 1
                            await emit(
                                "playback.finished",
                                {
                                    "index": next_index,
                                    "audio_id": result.audio_id,
                                    "segment_id": result.segment_id,
                                },
                            )
                        finally:
                            await self._tts.discard(result)
                            cleanup.pop(result.audio_id, None)
                        next_index += 1
                finally:
                    audio_queue.task_done()

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(stream_to_tts())
                if audio_enabled:
                    for _ in range(self._tts_worker_count):
                        group.create_task(tts_worker())
                    group.create_task(ordered_playback())
            token.raise_if_cancelled()
            metrics.turn_total_ms = _elapsed_ms(started)
            return TurnOutcome(
                full_text="".join(full_text_parts),
                segments=completed_segments,
                metrics=metrics,
            )
        finally:
            if audio_enabled:
                await self._audio_player.stop(immediate=token.cancelled)
            if cleanup:
                await asyncio.gather(
                    *(self._tts.discard(result) for result in tuple(cleanup.values())),
                    return_exceptions=True,
                )

    async def _queue_segment(
        self,
        segment: DialogueSegment,
        token: CancellationToken,
        metrics: TurnMetrics,
        completed_segments: list[DialogueSegment],
        started: float,
        emit: EventEmitter,
        queue: asyncio.Queue[_IndexedJob | None] | None,
    ) -> None:
        token.raise_if_cancelled()
        if self._segment_decorator is not None:
            segment = self._segment_decorator(segment)
        if metrics.llm_first_segment_ms is None:
            metrics.llm_first_segment_ms = _elapsed_ms(started)
        metrics.segment_count += 1
        completed_segments.append(segment)
        await emit(
            "assistant.segment",
            {**segment.model_dump(mode="json"), "is_final": True},
        )
        if queue is None:
            return
        job = TTSJob(
            turn_id=segment.turn_id,
            segment_id=segment.segment_id,
            text=segment.text,
            style=segment.tts_style,
            emotion=segment.emotion,
            speed_factor=segment.tts_speed_factor,
            interruptible=segment.interruptible,
            cancellation_token_id=token.token_id,
        )
        await emit(
            "tts.job",
            {"index": segment.index, "job_id": job.job_id, "segment_id": segment.segment_id},
        )
        await queue.put(_IndexedJob(index=segment.index, job=job))

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self._close_resources(), name="dialogue-pipeline-close"
                )
            task = self._close_task
        await asyncio.shield(task)

    async def _close_resources(self) -> None:
        results = await asyncio.gather(
            self._audio_player.close(),
            self._tts.close(),
            self._llm.close(),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("dialogue pipeline resource close failed", errors)


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
