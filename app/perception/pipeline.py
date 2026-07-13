"""Fail-closed Guard → capture → OCR → redact → optional cloud pipeline."""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TypeVar

from app.perception.guards import TextRedactor
from app.perception.models import (
    FrameChangeAssessment,
    GuardOutcome,
    ImageFrame,
    ObservationResult,
    ObservationStatus,
    OCRResult,
    Rect,
    SceneAnalysis,
)
from app.perception.protocols import (
    ActiveWindowSource,
    ChangeDetector,
    CloudVisionAnalyzer,
    ContentGuard,
    ImageSanitizer,
    OCRProvider,
    RateLimiter,
    SceneClassifier,
    WindowCapture,
    WindowGuard,
)
from app.schemas.ai import PerceptionContext

ResultT = TypeVar("ResultT")

_SAFE_CATEGORIES = {
    "unknown",
    "coding",
    "editing",
    "game",
    "reading",
    "browser",
    "video",
    "idle",
}


@dataclass(frozen=True, slots=True)
class PerceptionPipelineConfig:
    operation_timeout_seconds: float = 10.0
    max_frame_bytes: int = 20 * 1024 * 1024
    always_redact_regions: tuple[Rect, ...] = ()

    def __post_init__(self) -> None:
        if self.operation_timeout_seconds <= 0 or self.max_frame_bytes < 1:
            raise ValueError("感知超时和图片大小限制必须大于 0")


class PerceptionPipeline:
    def __init__(
        self,
        *,
        enabled: Callable[[], bool],
        cloud_enabled: Callable[[], bool] | None = None,
        window_source: ActiveWindowSource,
        window_guard: WindowGuard,
        capture: WindowCapture,
        change_detector: ChangeDetector,
        ocr: OCRProvider,
        content_guard: ContentGuard,
        classifier: SceneClassifier,
        redactor: TextRedactor,
        sanitizer: ImageSanitizer | None = None,
        cloud: CloudVisionAnalyzer | None = None,
        cloud_limiter: RateLimiter | None = None,
        config: PerceptionPipelineConfig | None = None,
    ) -> None:
        if (sanitizer is None) != (cloud is None):
            raise ValueError("cloud vision 与 image sanitizer 必须同时配置")
        self._enabled = enabled
        self._cloud_enabled = cloud_enabled if cloud_enabled is not None else (lambda: False)
        self._window_source = window_source
        self._window_guard = window_guard
        self._capture = capture
        self._change_detector = change_detector
        self._ocr = ocr
        self._content_guard = content_guard
        self._classifier = classifier
        self._redactor = redactor
        self._sanitizer = sanitizer
        self._cloud = cloud
        self._cloud_limiter = cloud_limiter
        self._config = config or PerceptionPipelineConfig()

        self._closed = False
        self._close_finished = False
        self._vision_permitted = True
        self._cloud_permitted = True
        self._vision_generation = 0
        self._feature_state_lock = threading.Lock()
        self._active_observations: set[asyncio.Task[object]] = set()
        self._lifecycle_cancelled: set[asyncio.Task[object]] = set()
        self._transition_tasks: set[asyncio.Task[None]] = set()
        self._observation_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._feature_unsubscribe: Callable[[], None] | None = None

    async def observe(self) -> ObservationResult:
        self._bind_loop()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("感知观察必须运行在 asyncio task 中")
        if not self._is_enabled():
            return self._disabled_result()
        self._active_observations.add(task)
        try:
            async with self._observation_lock:
                if not self._is_enabled():
                    return self._disabled_result()
                return await self._observe_once()
        except asyncio.CancelledError:
            if task in self._lifecycle_cancelled:
                return self._disabled_result()
            raise
        finally:
            self._active_observations.discard(task)
            self._lifecycle_cancelled.discard(task)

    async def _observe_once(self) -> ObservationResult:
        try:
            window = await self._with_timeout(self._window_source.active_window())
            if not self._is_enabled():
                return self._disabled_result()
            decision = await self._with_timeout(self._window_guard.evaluate(window))
            if not self._is_enabled():
                return self._disabled_result()
        except Exception:
            return ObservationResult(
                status=ObservationStatus.guard_error,
                reason_code="pre_capture_guard_failed",
            )
        if decision.outcome is GuardOutcome.block:
            return ObservationResult(
                status=ObservationStatus.blocked_before_capture,
                context=self._sensitive_context(),
                reason_code=decision.reason_code,
            )
        if not self._is_enabled():
            return self._disabled_result()

        frame: ImageFrame | None = None
        sanitized: ImageFrame | None = None
        ocr_result: OCRResult | None = None
        ocr_redaction_regions: tuple[Rect, ...] | None = ()
        assessment: FrameChangeAssessment | None = None
        try:
            try:
                frame = await self._with_timeout(self._capture.capture(window))
                if not self._is_enabled():
                    return self._disabled_result()
                if len(frame.data) > self._config.max_frame_bytes:
                    raise ValueError("captured frame too large")
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.capture_error,
                    reason_code="capture_failed",
                )
            try:
                assessment = self._change_detector.compare(window, frame)
                if not assessment.changed:
                    self._change_detector.commit(
                        assessment,
                        sensitive=assessment.previous_sensitive,
                    )
                    return ObservationResult(
                        status=ObservationStatus.unchanged,
                        context=(
                            self._sensitive_context() if assessment.previous_sensitive else None
                        ),
                        reason_code=(
                            "sticky_sensitive_context" if assessment.previous_sensitive else None
                        ),
                    )
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.processing_error,
                    reason_code="change_detection_failed",
                )
            assert assessment is not None
            try:
                ocr_result = await self._with_timeout(self._ocr.extract(frame))
                if not self._is_enabled():
                    return self._disabled_result()
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.ocr_error,
                    reason_code="ocr_failed",
                )
            try:
                content_decision = self._content_guard.evaluate(ocr_result)
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.guard_error,
                    reason_code="content_guard_failed",
                )
            if content_decision.outcome is GuardOutcome.block:
                # The sensitive result remains authoritative. A failed cache
                # commit simply forces local OCR to run again.
                with suppress(Exception):
                    self._change_detector.commit(assessment, sensitive=True)
                return ObservationResult(
                    status=ObservationStatus.blocked_after_ocr,
                    context=self._sensitive_context(),
                    reason_code=(
                        content_decision.reason_codes[0]
                        if content_decision.reason_codes
                        else "sensitive_content"
                    ),
                )
            if not self._is_enabled():
                return self._disabled_result()
            ocr_redaction_regions = self._ocr_redaction_regions(ocr_result, frame)
            try:
                local = self._classifier.classify(window, ocr_result)
                local_context = self._context_from_analysis(local)
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.processing_error,
                    reason_code="scene_classification_failed",
                )

            # Raw OCR and window metadata are no longer needed once local
            # privacy and coarse scene state are known.
            ocr_result.wipe()
            ocr_result = None
            del window

            if self._cloud is None or self._sanitizer is None or not self._is_cloud_enabled():
                return self._safe_result(
                    assessment,
                    status=ObservationStatus.analyzed_local,
                    context=local_context,
                )
            if ocr_redaction_regions is None:
                return self._safe_result(
                    assessment,
                    status=ObservationStatus.analyzed_local,
                    context=local_context,
                    reason_code="cloud_ocr_redaction_unavailable",
                )
            if self._cloud_limiter is not None and not self._cloud_limiter.allow():
                return self._safe_result(
                    assessment,
                    status=ObservationStatus.cloud_rate_limited,
                    context=local_context,
                )
            if not self._is_enabled():
                return self._disabled_result()
            if not self._is_cloud_enabled():
                return self._safe_result(
                    assessment,
                    status=ObservationStatus.analyzed_local,
                    context=local_context,
                )
            regions = (
                *self._config.always_redact_regions,
                *content_decision.redaction_regions,
                *ocr_redaction_regions,
            )
            try:
                sanitized = await self._with_timeout(self._sanitizer.sanitize(frame, regions))
                if sanitized is frame:
                    raise ValueError("sanitizer must return a new frame")
            except Exception:
                return self._safe_result(
                    assessment,
                    status=ObservationStatus.analyzed_local,
                    context=local_context,
                    reason_code="cloud_redaction_failed",
                )
            try:
                frame.wipe()
                frame = None
                if not self._is_enabled():
                    return self._disabled_result()
                if not self._is_cloud_enabled():
                    return self._safe_result(
                        assessment,
                        status=ObservationStatus.analyzed_local,
                        context=local_context,
                    )
                cloud = await self._with_timeout(self._cloud.analyze(sanitized, local))
                sanitized.wipe()
                sanitized = None
                if not self._is_enabled():
                    return self._disabled_result()
                if not self._is_cloud_enabled():
                    return self._safe_result(
                        assessment,
                        status=ObservationStatus.analyzed_local,
                        context=local_context,
                    )
                if not math.isfinite(cloud.confidence):
                    raise ValueError("invalid cloud confidence")
                cloud_summary = self._redactor.redact(cloud.summary)
                cloud_context = PerceptionContext(
                    category=self._safe_category(cloud.category, fallback=local.category),
                    summary=cloud_summary,
                    confidence=min(1.0, max(0.0, cloud.confidence)),
                    sensitive=False,
                )
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.cloud_error,
                    reason_code="cloud_processing_failed",
                )
            return self._safe_result(
                assessment,
                status=ObservationStatus.analyzed_cloud,
                context=cloud_context,
            )
        finally:
            if ocr_result is not None:
                ocr_result.wipe()
            if sanitized is not None:
                sanitized.wipe()
            if frame is not None:
                frame.wipe()

    async def _with_timeout(self, awaitable: Awaitable[ResultT]) -> ResultT:
        async with asyncio.timeout(self._config.operation_timeout_seconds):
            return await awaitable

    def _is_enabled(self) -> bool:
        with self._feature_state_lock:
            vision_permitted = self._vision_permitted
        if self._closed or not vision_permitted:
            return False
        try:
            return self._enabled()
        except Exception:
            return False

    def _is_cloud_enabled(self) -> bool:
        with self._feature_state_lock:
            cloud_permitted = self._cloud_permitted
        if self._closed or not cloud_permitted:
            return False
        try:
            return self._cloud_enabled()
        except Exception:
            return False

    @staticmethod
    def _safe_category(value: str | None, *, fallback: str) -> str:
        normalized = value.strip().casefold() if isinstance(value, str) else ""
        return normalized if normalized in _SAFE_CATEGORIES else fallback

    @staticmethod
    def _ocr_redaction_regions(
        ocr: OCRResult,
        frame: ImageFrame,
    ) -> tuple[Rect, ...] | None:
        regions: list[Rect] = []
        seen: set[Rect] = set()
        for span in ocr.spans:
            if not span.text:
                continue
            box = span.box
            if (
                box is None
                or box.x < 0
                or box.y < 0
                or box.x + box.width > frame.width
                or box.y + box.height > frame.height
            ):
                return None
            if box not in seen:
                seen.add(box)
                regions.append(box)
        return tuple(regions)

    def _context_from_analysis(self, analysis: SceneAnalysis) -> PerceptionContext:
        return PerceptionContext(
            category=analysis.category,
            summary=self._redactor.redact(analysis.summary),
            confidence=analysis.confidence,
            sensitive=False,
        )

    def _safe_result(
        self,
        assessment: FrameChangeAssessment,
        *,
        status: ObservationStatus,
        context: PerceptionContext,
        reason_code: str | None = None,
    ) -> ObservationResult:
        """Commit only a completed, locally privacy-checked observation."""

        if not self._is_enabled():
            return self._disabled_result()
        try:
            self._change_detector.commit(assessment, sensitive=False)
        except Exception:
            return ObservationResult(
                status=ObservationStatus.processing_error,
                reason_code="change_commit_failed",
            )
        return ObservationResult(status=status, context=context, reason_code=reason_code)

    @staticmethod
    def _sensitive_context() -> PerceptionContext:
        return PerceptionContext(
            category="sensitive",
            summary="当前屏幕场景已被隐私保护规则拦截。",
            confidence=1.0,
            sensitive=True,
        )

    def attach_feature_subscription(self, unsubscribe: Callable[[], None]) -> None:
        if self._feature_unsubscribe is not None:
            raise RuntimeError("感知 feature subscription 已配置")
        self._feature_unsubscribe = unsubscribe

    def notify_vision_enabled(self, enabled: bool) -> None:
        """Thread-safe listener entry; off-loop disable waits for cleanup."""

        generation = self._set_vision_permitted(enabled)
        if enabled:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            self._change_detector.reset()
            return
        if threading.get_ident() == self._loop_thread_id:
            self._track_transition(loop.create_task(self._finish_vision_disable(generation)))
            return
        future = asyncio.run_coroutine_threadsafe(
            self._finish_vision_disable(generation),
            loop,
        )
        future.result()

    def notify_cloud_enabled(self, enabled: bool) -> None:
        with self._feature_state_lock:
            self._cloud_permitted = enabled

    async def set_vision_enabled(self, enabled: bool) -> None:
        """Apply vision state and await cancellation of active observations."""

        self._bind_loop()
        generation = self._set_vision_permitted(enabled)
        if enabled:
            return
        await self._finish_vision_disable(generation)

    async def _finish_vision_disable(self, generation: int) -> None:
        with self._feature_state_lock:
            if self._vision_permitted or generation != self._vision_generation:
                return
        try:
            await self._cancel_active_observations()
        finally:
            self._change_detector.reset()

    async def close(self) -> None:
        self._bind_loop()
        async with self._close_lock:
            if self._close_finished:
                return
            self._closed = True
            with self._feature_state_lock:
                self._vision_generation += 1
                self._vision_permitted = False
                self._cloud_permitted = False
            unsubscribe = self._feature_unsubscribe
            self._feature_unsubscribe = None
            if unsubscribe is not None:
                with suppress(Exception):
                    unsubscribe()
            await self._cancel_active_observations()
            transitions = tuple(
                task for task in self._transition_tasks if task is not asyncio.current_task()
            )
            if transitions:
                await asyncio.gather(*transitions, return_exceptions=True)
            self._change_detector.reset()
            closers: list[Awaitable[None]] = [self._ocr.close()]
            sanitizer_close = getattr(self._sanitizer, "close", None)
            if callable(sanitizer_close):
                closers.append(sanitizer_close())
            results = await asyncio.gather(*closers, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            self._close_finished = True

    async def _cancel_active_observations(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(task for task in self._active_observations if task is not current)
        self._lifecycle_cancelled.update(tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _disabled_result(self) -> ObservationResult:
        self._change_detector.reset()
        return ObservationResult(status=ObservationStatus.disabled)

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._loop_thread_id = threading.get_ident()
        elif self._loop is not loop:
            raise RuntimeError("PerceptionPipeline 不能跨 event loop 使用")

    def _track_transition(self, task: asyncio.Task[None]) -> None:
        self._transition_tasks.add(task)
        task.add_done_callback(self._transition_tasks.discard)

    def _set_vision_permitted(self, enabled: bool) -> int:
        with self._feature_state_lock:
            self._vision_generation += 1
            self._vision_permitted = enabled
            return self._vision_generation
