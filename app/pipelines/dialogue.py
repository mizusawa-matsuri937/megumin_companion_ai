"""Mock streaming -> segmentation -> concurrent TTS -> ordered playback pipeline."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from app.clients.llm import LLMProvider
from app.clients.tts import TTSProvider
from app.core.cancellation import CancellationToken
from app.core.context import (
    ContextBuilder,
    DirectContextBuilder,
    DirectProactiveContextBuilder,
    ProactiveContextBuilder,
)
from app.limits import LimitsConfig
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
_ItemT = TypeVar("_ItemT")
_QUEUE_END = object()


@dataclass(frozen=True, slots=True)
class QueueResourceReport:
    capacity: int
    max_depth: int
    final_depth: int
    producer_block_count: int
    producer_block_ms: int


@dataclass(frozen=True, slots=True)
class PipelineResourceReport:
    tts_queue: QueueResourceReport
    ready_audio_queue: QueueResourceReport
    max_reorder_depth: int
    max_audio_inflight_bytes: int
    final_audio_inflight_bytes: int
    max_output_utf8_bytes: int
    max_temp_bytes: int
    cleanup_ms: int
    terminal: str
    reason: str | None


class _MeasuredQueue(Generic[_ItemT]):
    """One bounded queue with one logical producer owner and explicit close/drain."""

    def __init__(self, capacity: int) -> None:
        self._queue: asyncio.Queue[_ItemT | object] = asyncio.Queue(maxsize=capacity)
        self._capacity = capacity
        self._max_depth = 0
        self._producer_block_count = 0
        self._producer_block_ms = 0
        self._closed = False

    async def put(self, item: _ItemT) -> None:
        blocked = self._queue.full()
        started = time.perf_counter()
        await self._queue.put(item)
        if blocked:
            self._producer_block_count += 1
            self._producer_block_ms += _elapsed_ms(started)
        self._max_depth = max(self._max_depth, self._queue.qsize())

    async def close(self, consumer_count: int) -> None:
        if self._closed:
            return
        self._closed = True
        for _ in range(consumer_count):
            await self._queue.put(_QUEUE_END)
            self._max_depth = max(self._max_depth, self._queue.qsize())

    async def get(self) -> _ItemT | object:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def drain(self) -> list[_ItemT]:
        items: list[_ItemT] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if item is not _QUEUE_END:
                items.append(cast(_ItemT, item))
        return items

    def report(self) -> QueueResourceReport:
        return QueueResourceReport(
            capacity=self._capacity,
            max_depth=self._max_depth,
            final_depth=self._queue.qsize(),
            producer_block_count=self._producer_block_count,
            producer_block_ms=self._producer_block_ms,
        )


class _AudioByteBudget:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._used = 0
        self._max_used = 0
        self._condition = asyncio.Condition()

    @property
    def max_used(self) -> int:
        return self._max_used

    @property
    def used(self) -> int:
        return self._used

    async def acquire(self, amount: int) -> None:
        if amount > self._capacity:
            raise ValueError("audio reservation exceeds in-flight hard limit")
        async with self._condition:
            await self._condition.wait_for(lambda: self._used + amount <= self._capacity)
            self._used += amount
            self._max_used = max(self._max_used, self._used)

    async def shrink(self, reserved: int, actual: int) -> None:
        if actual > reserved:
            raise ValueError("audio result exceeds its reserved hard limit")
        await self.release(reserved - actual)

    async def release(self, amount: int) -> None:
        if amount <= 0:
            return
        async with self._condition:
            self._used = max(0, self._used - amount)
            self._condition.notify_all()


@dataclass(slots=True)
class _IndexedJob:
    index: int
    job: TTSJob


@dataclass(slots=True)
class _IndexedAudio:
    index: int
    result: AudioResult
    ready_at: float
    leased_bytes: int = 0
    slot_owned: bool = True


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
        limits: LimitsConfig | None = None,
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
        self._limits = limits or LimitsConfig()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._last_resource_report: PipelineResourceReport | None = None

    @property
    def last_resource_report(self) -> PipelineResourceReport | None:
        return self._last_resource_report

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
        tts_queue = _MeasuredQueue[_IndexedJob](self._limits.tts_queue_capacity)
        audio_queue = _MeasuredQueue[_IndexedAudio](self._limits.ready_audio_queue_capacity)
        ready_slots = asyncio.Semaphore(self._limits.ready_audio_queue_capacity)
        audio_budget = _AudioByteBudget(self._limits.audio_inflight_bytes)
        cleanup: dict[str, AudioResult] = {}
        terminal = "completed"
        terminal_reason: str | None = None
        total_output_bytes = 0
        total_audio_duration_ms = 0
        max_reorder_depth = 0
        workers_remaining = self._tts_worker_count
        workers_clean = True
        worker_group_lock = asyncio.Lock()

        async def stream_to_tts() -> None:
            nonlocal terminal, terminal_reason, total_output_bytes
            segmenter = DialogueSegmenter(
                state.turn_id,
                min_chars=self._segment_min_chars,
                max_chars=self._segment_max_chars,
                max_words=self._segment_max_words,
            )
            stream = self._llm.stream(request, token)
            clean_exit = False
            try:
                async for delta in stream:
                    token.raise_if_cancelled()
                    remaining = self._limits.llm_output_bytes - total_output_bytes
                    accepted, cut = _utf8_prefix(delta, remaining)
                    if accepted:
                        full_text_parts.append(accepted)
                        total_output_bytes += len(accepted.encode("utf-8"))
                    if not accepted and delta:
                        cut = True
                    if metrics.llm_first_token_ms is None:
                        metrics.llm_first_token_ms = _elapsed_ms(started)
                    if accepted:
                        await emit("assistant.delta", {"delta": accepted})
                    for segment in segmenter.feed(accepted):
                        if metrics.segment_count >= self._limits.llm_output_segments:
                            terminal = "truncated"
                            terminal_reason = "output_segments"
                            break
                        await self._queue_segment(
                            segment,
                            token,
                            metrics,
                            completed_segments,
                            started,
                            emit,
                            tts_queue if audio_enabled else None,
                        )
                        if metrics.segment_count >= self._limits.llm_output_segments:
                            terminal = "truncated"
                            terminal_reason = "output_segments"
                            break
                    if terminal_reason == "output_segments":
                        break
                    if cut or total_output_bytes >= self._limits.llm_output_bytes:
                        terminal = "truncated"
                        terminal_reason = "output_bytes"
                        break
                token.raise_if_cancelled()
                if terminal_reason != "output_segments":
                    for segment in segmenter.flush():
                        if metrics.segment_count >= self._limits.llm_output_segments:
                            terminal = "truncated"
                            terminal_reason = terminal_reason or "output_segments"
                            break
                        await self._queue_segment(
                            segment,
                            token,
                            metrics,
                            completed_segments,
                            started,
                            emit,
                            tts_queue if audio_enabled else None,
                        )
                        if metrics.segment_count >= self._limits.llm_output_segments:
                            terminal = "truncated"
                            terminal_reason = terminal_reason or "output_segments"
                            break
                if terminal == "truncated":
                    await emit("assistant.truncated", {"reason": terminal_reason})
                clean_exit = True
            finally:
                closer = getattr(stream, "aclose", None)
                if closer is not None:
                    await closer()
                if clean_exit and audio_enabled:
                    await tts_queue.close(self._tts_worker_count)

        async def tts_worker() -> None:
            nonlocal workers_remaining, workers_clean, total_audio_duration_ms
            clean_exit = False
            try:
                while True:
                    indexed_job = await tts_queue.get()
                    try:
                        if indexed_job is _QUEUE_END:
                            clean_exit = True
                            return
                        indexed_job = cast(_IndexedJob, indexed_job)
                        token.raise_if_cancelled()
                        slot_owned = False
                        reservation = 0
                        leased_bytes = 0
                        transferred = False
                        await ready_slots.acquire()
                        slot_owned = True
                        await audio_budget.acquire(self._limits.audio_single_result_bytes)
                        reservation = self._limits.audio_single_result_bytes
                        synthesized_at = time.perf_counter()
                        try:
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
                                actual_bytes = _audio_size(result.audio_path)
                                duration_ms = result.duration_ms or 0
                                degradation: str | None = None
                                if actual_bytes < 44:
                                    degradation = "audio_unavailable"
                                elif actual_bytes > reservation:
                                    degradation = "audio_result_too_large"
                                elif (
                                    total_audio_duration_ms + duration_ms
                                    > self._limits.audio_total_duration_ms
                                ):
                                    degradation = "audio_duration_limit"
                                if degradation is not None:
                                    await self._tts.discard(result)
                                    await emit(
                                        "audio.degraded",
                                        {"index": indexed_job.index, "reason": degradation},
                                    )
                                    result = _audio_failure(result, degradation)
                                else:
                                    total_audio_duration_ms += duration_ms
                                    await audio_budget.shrink(reservation, actual_bytes)
                                    leased_bytes = actual_bytes
                                    reservation = 0
                                    cleanup[result.audio_id] = result
                                    if metrics.tts_first_audio_ms is None:
                                        metrics.tts_first_audio_ms = _elapsed_ms(started)
                            if not result.success and reservation:
                                await audio_budget.release(reservation)
                                reservation = 0
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
                                    leased_bytes=leased_bytes,
                                )
                            )
                            transferred = True
                        finally:
                            if not transferred:
                                await audio_budget.release(reservation + leased_bytes)
                                if slot_owned:
                                    ready_slots.release()
                    finally:
                        tts_queue.task_done()
            finally:
                async with worker_group_lock:
                    workers_remaining -= 1
                    workers_clean = workers_clean and clean_exit
                    if workers_remaining == 0 and workers_clean:
                        await audio_queue.close(1)

        async def ordered_playback() -> None:
            nonlocal max_reorder_depth
            pending: dict[int, _IndexedAudio] = {}
            next_index = 0
            try:
                while True:
                    indexed_audio = await audio_queue.get()
                    try:
                        if indexed_audio is _QUEUE_END:
                            return
                        indexed_audio = cast(_IndexedAudio, indexed_audio)
                        pending[indexed_audio.index] = indexed_audio
                        max_reorder_depth = max(max_reorder_depth, len(pending))
                    finally:
                        audio_queue.task_done()
                    while next_index in pending:
                        token.raise_if_cancelled()
                        current = pending.pop(next_index)
                        result = current.result
                        try:
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
                            await release_audio(current)
                        next_index += 1
            finally:
                await asyncio.gather(
                    *(release_audio(item) for item in tuple(pending.values())),
                    return_exceptions=True,
                )

        async def release_audio(indexed: _IndexedAudio) -> None:
            try:
                if indexed.result.success:
                    await self._tts.discard(indexed.result)
                    cleanup.pop(indexed.result.audio_id, None)
            finally:
                await audio_budget.release(indexed.leased_bytes)
                indexed.leased_bytes = 0
                if indexed.slot_owned:
                    indexed.slot_owned = False
                    ready_slots.release()

        succeeded = False
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(stream_to_tts())
                if audio_enabled:
                    for _ in range(self._tts_worker_count):
                        group.create_task(tts_worker())
                    group.create_task(ordered_playback())
            token.raise_if_cancelled()
            succeeded = True
        finally:
            cleanup_started = time.perf_counter()

            async def settle_turn() -> None:
                try:
                    if audio_enabled:
                        await self._audio_player.stop(immediate=token.cancelled)
                finally:
                    drained = audio_queue.drain()
                    tts_queue.drain()
                    await asyncio.gather(
                        *(release_audio(indexed) for indexed in drained),
                        return_exceptions=True,
                    )
                    if cleanup:
                        await asyncio.gather(
                            *(self._tts.discard(result) for result in tuple(cleanup.values())),
                            return_exceptions=True,
                        )
                        cleanup.clear()

            await _finish_cleanup(settle_turn())
            cleanup_ms = _elapsed_ms(cleanup_started)
            observed_terminal = terminal
            observed_reason = terminal_reason
            if not succeeded:
                observed_terminal = "cancelled" if token.cancelled else "failed"
                observed_reason = "turn_cancelled" if token.cancelled else "pipeline_failed"
            report = PipelineResourceReport(
                tts_queue=tts_queue.report(),
                ready_audio_queue=audio_queue.report(),
                max_reorder_depth=max_reorder_depth,
                max_audio_inflight_bytes=audio_budget.max_used,
                final_audio_inflight_bytes=audio_budget.used,
                max_output_utf8_bytes=total_output_bytes,
                max_temp_bytes=audio_budget.max_used,
                cleanup_ms=cleanup_ms,
                terminal=observed_terminal,
                reason=observed_reason,
            )
            self._last_resource_report = report
            metrics.tts_queue_capacity = report.tts_queue.capacity
            metrics.tts_queue_max_depth = report.tts_queue.max_depth
            metrics.tts_producer_block_count = report.tts_queue.producer_block_count
            metrics.tts_producer_block_ms = report.tts_queue.producer_block_ms
            metrics.ready_audio_queue_capacity = report.ready_audio_queue.capacity
            metrics.ready_audio_queue_max_depth = report.ready_audio_queue.max_depth
            metrics.audio_producer_block_count = report.ready_audio_queue.producer_block_count
            metrics.audio_producer_block_ms = report.ready_audio_queue.producer_block_ms
            metrics.max_audio_inflight_bytes = report.max_audio_inflight_bytes
            metrics.final_audio_inflight_bytes = report.final_audio_inflight_bytes
            metrics.max_output_utf8_bytes = report.max_output_utf8_bytes
            metrics.max_temp_bytes = report.max_temp_bytes
            metrics.cleanup_ms = cleanup_ms

        metrics.turn_total_ms = _elapsed_ms(started)
        return TurnOutcome(
            full_text="".join(full_text_parts),
            segments=completed_segments,
            metrics=metrics,
            metadata={"terminal": terminal, "reason": terminal_reason},
        )

    async def _queue_segment(
        self,
        segment: DialogueSegment,
        token: CancellationToken,
        metrics: TurnMetrics,
        completed_segments: list[DialogueSegment],
        started: float,
        emit: EventEmitter,
        queue: _MeasuredQueue[_IndexedJob] | None,
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


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    if max_bytes <= 0:
        return "", True
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _audio_size(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _audio_failure(result: AudioResult, error_code: str) -> AudioResult:
    return AudioResult(
        job_id=result.job_id,
        turn_id=result.turn_id,
        segment_id=result.segment_id,
        success=False,
        error_code=error_code,
    )


async def _finish_cleanup(awaitable: Awaitable[None]) -> None:
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    task.result()
    if cancelled:
        raise asyncio.CancelledError
