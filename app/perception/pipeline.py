"""Fail-closed Guard → capture → OCR → redact → optional cloud pipeline."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.perception.guards import TextRedactor
from app.perception.models import (
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

    async def observe(self) -> ObservationResult:
        if self._closed or not self._is_enabled():
            return ObservationResult(status=ObservationStatus.disabled)
        try:
            window = await self._with_timeout(self._window_source.active_window())
            if not self._is_enabled():
                return ObservationResult(status=ObservationStatus.disabled)
            decision = await self._with_timeout(self._window_guard.evaluate(window))
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
            return ObservationResult(status=ObservationStatus.disabled)

        frame: ImageFrame | None = None
        sanitized: ImageFrame | None = None
        ocr_result: OCRResult | None = None
        try:
            try:
                frame = await self._with_timeout(self._capture.capture(window))
                if len(frame.data) > self._config.max_frame_bytes:
                    raise ValueError("captured frame too large")
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.capture_error,
                    reason_code="capture_failed",
                )
            if not self._is_enabled():
                return ObservationResult(status=ObservationStatus.disabled)
            try:
                if not self._change_detector.should_analyze(window, frame):
                    return ObservationResult(status=ObservationStatus.unchanged)
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.processing_error,
                    reason_code="change_detection_failed",
                )
            try:
                ocr_result = await self._with_timeout(self._ocr.extract(frame))
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
                return ObservationResult(
                    status=ObservationStatus.blocked_after_ocr,
                    context=self._sensitive_context(),
                    reason_code=content_decision.reason_codes[0]
                    if content_decision.reason_codes
                    else "sensitive_content",
                )
            if not self._is_enabled():
                return ObservationResult(status=ObservationStatus.disabled)
            try:
                local = self._classifier.classify(window, ocr_result)
                local_context = self._context_from_analysis(local)
            except Exception:
                return ObservationResult(
                    status=ObservationStatus.processing_error,
                    reason_code="scene_classification_failed",
                )

            if self._cloud is None or self._sanitizer is None:
                return ObservationResult(
                    status=ObservationStatus.analyzed_local,
                    context=local_context,
                )
            if self._cloud_limiter is not None and not self._cloud_limiter.allow():
                return ObservationResult(
                    status=ObservationStatus.cloud_rate_limited,
                    context=local_context,
                )
            if not self._is_enabled():
                return ObservationResult(status=ObservationStatus.disabled)
            try:
                regions = (*self._config.always_redact_regions, *content_decision.redaction_regions)
                sanitized = await self._with_timeout(self._sanitizer.sanitize(frame, regions))
                if not self._is_enabled():
                    return ObservationResult(status=ObservationStatus.disabled)
                cloud = await self._with_timeout(self._cloud.analyze(sanitized, local))
                if not self._is_enabled():
                    return ObservationResult(status=ObservationStatus.disabled)
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
            return ObservationResult(
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
        try:
            return self._enabled()
        except Exception:
            return False

    @staticmethod
    def _safe_category(value: str | None, *, fallback: str) -> str:
        normalized = value.strip().casefold() if isinstance(value, str) else ""
        return normalized if normalized in _SAFE_CATEGORIES else fallback

    def _context_from_analysis(self, analysis: SceneAnalysis) -> PerceptionContext:
        return PerceptionContext(
            category=analysis.category,
            summary=self._redactor.redact(analysis.summary),
            confidence=analysis.confidence,
            sensitive=False,
        )

    @staticmethod
    def _sensitive_context() -> PerceptionContext:
        return PerceptionContext(
            category="sensitive",
            summary="当前屏幕场景已被隐私保护规则拦截。",
            confidence=1.0,
            sensitive=True,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._ocr.close()
