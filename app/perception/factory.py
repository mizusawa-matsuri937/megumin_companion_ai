"""Dependency-injected composition for platform-independent screen perception."""

from __future__ import annotations

from app.config import Settings
from app.core import FeatureFlagSource
from app.perception.change_detection import FrameChangeDetector
from app.perception.classification import LocalSceneClassifier
from app.perception.guards import OCRContentGuard, PreCaptureGuard, TextRedactor
from app.perception.image_processing import PillowImageSanitizer
from app.perception.ocr import RapidOCRProvider
from app.perception.pipeline import PerceptionPipeline, PerceptionPipelineConfig
from app.perception.protocols import (
    ActiveWindowSource,
    CloudVisionAnalyzer,
    ImageSanitizer,
    OCRProvider,
    WindowCapture,
)
from app.perception.rate_limit import SlidingWindowRateLimiter
from app.schemas import FeatureName


def build_perception_pipeline(
    settings: Settings,
    feature_flags: FeatureFlagSource,
    *,
    window_source: ActiveWindowSource,
    capture: WindowCapture,
    ocr: OCRProvider | None = None,
    cloud: CloudVisionAnalyzer | None = None,
    sanitizer: ImageSanitizer | None = None,
) -> PerceptionPipeline:
    """Compose guards around injected platform adapters; no capture happens here."""

    if cloud is None and sanitizer is not None:
        raise ValueError("没有 cloud analyzer 时不得单独配置 sanitizer")
    resolved_sanitizer = sanitizer
    if cloud is not None and resolved_sanitizer is None:
        resolved_sanitizer = PillowImageSanitizer()
    return PerceptionPipeline(
        enabled=lambda: feature_flags.get_feature(FeatureName.vision).enabled,
        cloud_enabled=lambda: feature_flags.get_feature(FeatureName.cloud_vision).enabled,
        window_source=window_source,
        window_guard=PreCaptureGuard(),
        capture=capture,
        change_detector=FrameChangeDetector(
            minimum_hash_distance=settings.perception.minimum_hash_distance,
            max_windows=settings.perception.max_tracked_windows,
        ),
        ocr=ocr
        or RapidOCRProvider(
            max_spans=settings.perception.ocr_max_spans,
            max_span_chars=settings.perception.ocr_max_span_chars,
        ),
        content_guard=OCRContentGuard(max_text_chars=settings.perception.max_ocr_text_chars),
        classifier=LocalSceneClassifier(),
        redactor=TextRedactor(max_chars=settings.perception.max_summary_chars),
        sanitizer=resolved_sanitizer,
        cloud=cloud,
        cloud_limiter=(
            SlidingWindowRateLimiter(
                max_calls=settings.perception.cloud_max_calls,
                period_seconds=settings.perception.cloud_period_seconds,
            )
            if cloud is not None
            else None
        ),
        config=PerceptionPipelineConfig(
            operation_timeout_seconds=settings.perception.operation_timeout_seconds,
            max_frame_bytes=settings.perception.max_frame_bytes,
        ),
    )
