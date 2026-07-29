"""Mock streaming -> segmentation -> concurrent TTS -> ordered playback pipeline."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from app.clients.llm import LLMProvider
from app.clients.llm.errors import LLMErrorCode, LLMProviderError
from app.clients.tts import TTSProvider
from app.core.cancellation import CancellationToken, TurnCancelledError
from app.core.context import (
    ContextBuilder,
    DirectContextBuilder,
    DirectProactiveContextBuilder,
    ProactiveContextBuilder,
)
from app.emotion import EmotionLabel, FocusedVariant, map_presentation
from app.limits import LimitsConfig
from app.pipelines.audio_player import AudioPlaybackResult, AudioPlayer
from app.pipelines.segmenter import DialogueSegmenter
from app.pipelines.structured_turn import (
    IncrementalTurnJSONParser,
    StructuredTurnPlan,
    StructuredTurnSegment,
    TurnPlanResolver,
    TurnStreamFormat,
    prepare_structured_turn_request,
    provider_turn_stream_format,
)
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


class _TurnCancellationSignal(Exception):
    """Make cooperative cancellation fail a TaskGroup instead of being ignored."""


async def _run_pipeline_stage(awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except TurnCancelledError as exc:
        raise _TurnCancellationSignal from exc


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
    segment: DialogueSegment


@dataclass(slots=True)
class _IndexedAudio:
    index: int
    result: AudioResult
    ready_at: float
    segment: DialogueSegment
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
        turn_plan_resolver: TurnPlanResolver | None = None,
        tts_worker_count: int = 2,
        segment_min_chars: int = 6,
        segment_max_chars: int = 42,
        segment_max_words: int = 25,
        limits: LimitsConfig | None = None,
        tts_connect_timeout_ms: int,
        tts_first_byte_timeout_ms: int,
        tts_total_timeout_ms: int,
        tts_cancellation_timeout_ms: int,
    ) -> None:
        if tts_worker_count < 1:
            raise ValueError("tts_worker_count 必须大于 0")
        if (
            min(
                tts_connect_timeout_ms,
                tts_first_byte_timeout_ms,
                tts_total_timeout_ms,
                tts_cancellation_timeout_ms,
            )
            <= 0
        ):
            raise ValueError("TTS deadlines must be positive")
        if max(tts_connect_timeout_ms, tts_first_byte_timeout_ms) > tts_total_timeout_ms:
            raise ValueError("TTS stage timeout cannot exceed total timeout")
        self._llm = llm
        self._tts = tts
        self._audio_player = audio_player
        self._context_builder = context_builder or DirectContextBuilder()
        self._proactive_context_builder = (
            proactive_context_builder or DirectProactiveContextBuilder()
        )
        self._segment_decorator = segment_decorator
        self._turn_plan_resolver = turn_plan_resolver
        self._tts_worker_count = tts_worker_count
        self._segment_min_chars = segment_min_chars
        self._segment_max_chars = segment_max_chars
        self._segment_max_words = segment_max_words
        self._limits = limits or LimitsConfig()
        self._tts_connect_timeout_ms = tts_connect_timeout_ms
        self._tts_first_byte_timeout_ms = tts_first_byte_timeout_ms
        self._tts_total_timeout_ms = tts_total_timeout_ms
        self._tts_cancellation_timeout_ms = tts_cancellation_timeout_ms
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
        structured_output = provider_turn_stream_format(self._llm) is TurnStreamFormat.avatar_json
        if structured_output:
            request = prepare_structured_turn_request(request)
        metrics = TurnMetrics()
        full_text_parts: list[str] = []
        completed_segments: list[DialogueSegment] = []
        tts_queue = _MeasuredQueue[_IndexedJob](self._limits.tts_queue_capacity)
        audio_queue = _MeasuredQueue[_IndexedAudio](self._limits.ready_audio_queue_capacity)
        ready_slots = asyncio.Semaphore(self._limits.ready_audio_queue_capacity)
        audio_budget = _AudioByteBudget(self._limits.audio_inflight_bytes)
        # Registry of every audio item after its queue slot is acquired.  The
        # queue is only a transport: cancellation may interrupt a producer or
        # consumer at a hand-off boundary, so final cleanup must not infer
        # ownership from queue contents alone.
        outstanding_audio: dict[int, _IndexedAudio] = {}
        terminal = "completed"
        terminal_reason: str | None = None
        total_output_bytes = 0
        total_audio_duration_ms = 0
        max_reorder_depth = 0
        workers_remaining = self._tts_worker_count
        workers_clean = True
        worker_group_lock = asyncio.Lock()
        playback_started = False
        resolved_plan: StructuredTurnPlan | None = None
        emitted_red_eye_segments = 0

        async def emit_visible_segment(segment: DialogueSegment) -> None:
            full_text_parts.append(segment.text)
            completed_segments.append(segment)
            await emit("assistant.delta", {"delta": segment.text})
            payload = segment.model_dump(
                mode="json",
                exclude={"focused_variant", "red_eye"},
            )
            await emit("assistant.segment", {**payload, "is_final": True})

        async def resolve_structured_plan(
            suggestion: StructuredTurnPlan,
        ) -> StructuredTurnPlan:
            nonlocal resolved_plan
            if resolved_plan is not None:
                return resolved_plan
            resolved = (
                self._turn_plan_resolver(suggestion)
                if self._turn_plan_resolver is not None
                else suggestion
            )
            if not isinstance(resolved, StructuredTurnPlan):
                raise LLMProviderError(LLMErrorCode.structured, retryable=False)
            resolved_plan = resolved
            await emit(
                "avatar.plan",
                {
                    "emotion": resolved.emotion.value,
                    "focused_variant": resolved.focused_variant.value,
                },
            )
            return resolved

        async def queue_structured_item(
            item: StructuredTurnSegment,
            segmenter: DialogueSegmenter,
            *,
            source_index: int,
        ) -> bool:
            nonlocal terminal, terminal_reason, total_output_bytes
            nonlocal emitted_red_eye_segments
            plan = resolved_plan
            if plan is None:
                raise LLMProviderError(LLMErrorCode.structured, retryable=False)
            encoded_size = len(item.text.encode("utf-8"))
            if total_output_bytes + encoded_size > self._limits.llm_output_bytes:
                terminal = "truncated"
                terminal_reason = "output_bytes"
                return False
            total_output_bytes += encoded_size
            forced_red_eye = source_index == 0 and (
                plan.emotion is EmotionLabel.explosion_mode
                or (
                    plan.emotion is EmotionLabel.focused
                    and plan.focused_variant is FocusedVariant.chuunibyou
                )
            )
            red_eye = item.red_eye or forced_red_eye
            pieces = [*segmenter.feed(item.text), *segmenter.flush()]
            for piece_index, piece in enumerate(pieces):
                if metrics.segment_count >= self._limits.llm_output_segments:
                    terminal = "truncated"
                    terminal_reason = "output_segments"
                    return False
                piece_red_eye = red_eye and piece_index == 0
                if piece_red_eye and emitted_red_eye_segments >= 8:
                    piece_red_eye = False
                if piece_red_eye:
                    emitted_red_eye_segments += 1
                presentation = map_presentation(plan.emotion)
                segment = piece.model_copy(
                    update={
                        "emotion": plan.emotion.value,
                        "tts_style": presentation.tts_style,
                        "tts_speed_factor": presentation.speed_factor,
                        "live2d_expression": presentation.vts_expression,
                        "expression_update": False,
                        "focused_variant": plan.focused_variant.value,
                        "red_eye": piece_red_eye,
                    }
                )
                segment = await self._queue_segment(
                    segment,
                    token,
                    metrics,
                    completed_segments,
                    started,
                    emit,
                    tts_queue if audio_enabled else None,
                    emit_immediately=False,
                )
                if not audio_enabled:
                    await emit_visible_segment(segment)
                    await emit("avatar.visual_fallback", {"index": segment.index})
                    if piece_red_eye:
                        await emit("avatar.red_eye", {"effect": "red_eye"})
            return True

        async def stream_to_tts() -> None:
            nonlocal terminal, terminal_reason, total_output_bytes
            stream = self._llm.stream(request, token)
            clean_exit = False
            try:
                if structured_output:
                    parser = IncrementalTurnJSONParser(
                        max_bytes=min(
                            128 * 1024,
                            self._limits.llm_output_bytes + 16 * 1024,
                        ),
                        max_segments=self._limits.llm_output_segments,
                    )
                    segmenter = DialogueSegmenter(
                        state.turn_id,
                        min_chars=self._segment_min_chars,
                        max_chars=self._segment_max_chars,
                        max_words=self._segment_max_words,
                    )
                    source_index = 0
                    async for delta in stream:
                        token.raise_if_cancelled()
                        if metrics.llm_first_token_ms is None and delta:
                            metrics.llm_first_token_ms = _elapsed_ms(started)
                        items = parser.feed(delta)
                        if parser.plan is not None:
                            await resolve_structured_plan(parser.plan)
                        for item in items:
                            if not await queue_structured_item(
                                item,
                                segmenter,
                                source_index=source_index,
                            ):
                                break
                            source_index += 1
                        if terminal_reason is not None:
                            break
                    if terminal_reason is None:
                        items = parser.finish()
                        if parser.plan is not None:
                            await resolve_structured_plan(parser.plan)
                        for item in items:
                            if not await queue_structured_item(
                                item,
                                segmenter,
                                source_index=source_index,
                            ):
                                break
                            source_index += 1
                else:
                    segmenter = DialogueSegmenter(
                        state.turn_id,
                        min_chars=self._segment_min_chars,
                        max_chars=self._segment_max_chars,
                        max_words=self._segment_max_words,
                    )
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
                try:
                    closer = getattr(stream, "aclose", None)
                    if closer is not None:
                        await closer()
                finally:
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
                            result = await self._tts.synthesize(
                                indexed_job.job,
                                segment_index=indexed_job.index,
                                token=token,
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
                                    if metrics.tts_first_audio_ms is None:
                                        metrics.tts_first_audio_ms = _elapsed_ms(started)
                            if not result.success and reservation:
                                await audio_budget.release(reservation)
                                reservation = 0
                            indexed_audio = _IndexedAudio(
                                index=indexed_job.index,
                                result=result,
                                ready_at=ready_at,
                                segment=indexed_job.segment,
                                leased_bytes=leased_bytes,
                            )
                            outstanding_audio[indexed_job.index] = indexed_audio
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
                            await audio_queue.put(indexed_audio)
                            transferred = True
                        finally:
                            if not transferred:
                                owned_audio = outstanding_audio.get(indexed_job.index)
                                if owned_audio is not None:
                                    await release_audio(owned_audio)
                                    reservation = 0
                                    leased_bytes = 0
                                    slot_owned = False
                                else:
                                    await _finish_cleanup(
                                        audio_budget.release(reservation + leased_bytes)
                                    )
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
            nonlocal max_reorder_depth, playback_started
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
                            if structured_output:
                                await emit_visible_segment(current.segment)
                            if not result.success:
                                if structured_output and current.segment.red_eye:
                                    await emit("avatar.red_eye", {"effect": "red_eye"})
                                await emit(
                                    "playback.skipped",
                                    {"index": next_index, "error_code": result.error_code},
                                )
                                next_index += 1
                                continue
                            metrics.audio_queue_wait_ms.append(
                                max(0, round((time.perf_counter() - current.ready_at) * 1000))
                            )
                            await emit(
                                "playback.started",
                                {
                                    "index": next_index,
                                    "audio_id": result.audio_id,
                                    "segment_id": result.segment_id,
                                },
                            )
                            if structured_output and current.segment.red_eye:
                                await emit("avatar.red_eye", {"effect": "red_eye"})
                            playback = _playback_result(
                                await self._audio_player.play(result, token)
                            )
                            token.raise_if_cancelled()
                            if playback.notice_code is not None:
                                await emit(
                                    "audio.degraded",
                                    {"index": next_index, "reason": playback.notice_code},
                                )
                            if not playback.played:
                                await emit(
                                    "playback.skipped",
                                    {
                                        "index": next_index,
                                        "error_code": playback.error_code or "audio_unavailable",
                                    },
                                )
                                next_index += 1
                                continue
                            if metrics.first_sentence_play_ms is None:
                                metrics.first_sentence_play_ms = _elapsed_ms(started)
                            playback_started = True
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
                await _finish_cleanup(
                    _gather_cleanup(*(release_audio(item) for item in tuple(pending.values())))
                )

        async def release_audio(indexed: _IndexedAudio) -> None:
            async def release_owned() -> None:
                if outstanding_audio.get(indexed.index) is not indexed:
                    return
                leased_bytes = indexed.leased_bytes
                indexed.leased_bytes = 0
                if indexed.slot_owned:
                    indexed.slot_owned = False
                    ready_slots.release()
                await audio_budget.release(leased_bytes)
                if indexed.result.success:
                    await self._tts.discard(indexed.result)
                outstanding_audio.pop(indexed.index, None)

            await _finish_cleanup(release_owned())

        succeeded = False
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(_run_pipeline_stage(stream_to_tts()))
                if audio_enabled:
                    for _ in range(self._tts_worker_count):
                        group.create_task(_run_pipeline_stage(tts_worker()))
                    group.create_task(_run_pipeline_stage(ordered_playback()))
            token.raise_if_cancelled()
            succeeded = True
        except ExceptionGroup as exc:
            if exc.subgroup(_TurnCancellationSignal) is not None:
                raise TurnCancelledError from None
            stable = _stable_provider_error(exc)
            if stable is None:
                raise
            code = _exception_code(stable)
            terminal = "failed"
            terminal_reason = code
            await emit(
                "assistant.output_incomplete",
                {
                    "error_code": code,
                    "visible_output": bool(full_text_parts),
                    "playback_started": playback_started,
                },
            )
            raise stable from None
        finally:
            cleanup_started = time.perf_counter()

            async def settle_turn() -> None:
                try:
                    if audio_enabled:
                        await self._audio_player.stop(immediate=token.cancelled)
                finally:
                    drained = audio_queue.drain()
                    tts_queue.drain()
                    await _gather_cleanup(*(release_audio(indexed) for indexed in drained))
                    if outstanding_audio:
                        await _gather_cleanup(
                            *(
                                release_audio(indexed)
                                for indexed in tuple(outstanding_audio.values())
                            )
                        )

            await _finish_cleanup(settle_turn())
            cleanup_ms = _elapsed_ms(cleanup_started)
            observed_terminal = terminal
            observed_reason = terminal_reason
            if not succeeded:
                observed_terminal = "cancelled" if token.cancelled else "failed"
                observed_reason = (
                    "turn_cancelled" if token.cancelled else terminal_reason or "pipeline_failed"
                )
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
        *,
        emit_immediately: bool = True,
    ) -> DialogueSegment:
        token.raise_if_cancelled()
        if self._segment_decorator is not None:
            segment = self._segment_decorator(segment)
        if metrics.llm_first_segment_ms is None:
            metrics.llm_first_segment_ms = _elapsed_ms(started)
        metrics.segment_count += 1
        if emit_immediately:
            completed_segments.append(segment)
            await emit(
                "assistant.segment",
                {**segment.model_dump(mode="json"), "is_final": True},
            )
        if queue is None:
            return segment
        job = TTSJob(
            turn_id=segment.turn_id,
            segment_id=segment.segment_id,
            text=segment.text,
            style=segment.tts_style,
            emotion=segment.emotion,
            speed_factor=segment.tts_speed_factor,
            interruptible=segment.interruptible,
            connect_timeout_ms=self._tts_connect_timeout_ms,
            first_byte_timeout_ms=self._tts_first_byte_timeout_ms,
            timeout_ms=self._tts_total_timeout_ms,
            cancellation_timeout_ms=self._tts_cancellation_timeout_ms,
            cancellation_token_id=token.token_id,
        )
        await emit(
            "tts.job",
            {"index": segment.index, "job_id": job.job_id, "segment_id": segment.segment_id},
        )
        await queue.put(_IndexedJob(index=segment.index, job=job, segment=segment))
        return segment

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


async def _gather_cleanup(*awaitables: Awaitable[None]) -> None:
    if not awaitables:
        return
    await asyncio.gather(*awaitables, return_exceptions=True)


def _stable_provider_error(group: BaseExceptionGroup[BaseException]) -> Exception | None:
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            nested = _stable_provider_error(error)
            if nested is not None:
                return nested
        elif isinstance(error, Exception) and _exception_code(error) != "pipeline_failed":
            return error
    return None


def _exception_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    value = getattr(code, "value", code)
    if isinstance(value, str) and 1 <= len(value) <= 128:
        return value
    return "pipeline_failed"


def _playback_result(value: object) -> AudioPlaybackResult:
    """Keep pre-W17 test doubles compatible while containing malformed results."""

    if value is None:
        return AudioPlaybackResult(played=True)
    if isinstance(value, AudioPlaybackResult):
        return value
    return AudioPlaybackResult(played=False, error_code="audio_playback_invalid")
