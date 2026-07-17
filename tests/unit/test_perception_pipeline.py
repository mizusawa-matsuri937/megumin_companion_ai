"""Order, fail-closed behavior, cancellation, and privacy cleanup tests."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from typing import Any

import pytest
from app.perception.change_detection import FrameChangeDetector
from app.perception.classification import LocalSceneClassifier
from app.perception.guards import OCRContentGuard, PreCaptureGuard, TextRedactor
from app.perception.image_processing import PillowImageSanitizer
from app.perception.models import (
    CloudAnalysis,
    ContentGuardDecision,
    FrameChangeAssessment,
    GuardOutcome,
    ImageFrame,
    ObservationStatus,
    OCRResult,
    OCRSpan,
    Rect,
    SceneAnalysis,
    WindowGuardDecision,
    WindowInfo,
)
from app.perception.pipeline import PerceptionPipeline, PerceptionPipelineConfig
from app.perception.protocols import CloudVisionAnalyzer, ImageSanitizer

_DEFAULT_OCR_BOX = Rect(1, 1, 10, 5)


class EnabledFlag:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def __call__(self) -> bool:
        return self.enabled


class RaisingEnabledFlag(EnabledFlag):
    def __call__(self) -> bool:
        raise RuntimeError("feature store unavailable")


class FakeWindowSource:
    def __init__(self, order: list[str], *, error: Exception | None = None) -> None:
        self.order = order
        self.error = error
        self.calls = 0

    async def active_window(self) -> WindowInfo:
        self.calls += 1
        self.order.append("window")
        if self.error is not None:
            raise self.error
        return WindowInfo("w1", "Ordinary", "Code.exe", Rect(0, 0, 100, 50))


class BlockingWindowSource(FakeWindowSource):
    async def active_window(self) -> WindowInfo:
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeWindowGuard:
    def __init__(
        self,
        order: list[str],
        *,
        outcome: GuardOutcome = GuardOutcome.allow,
        error: Exception | None = None,
    ) -> None:
        self.order = order
        self.outcome = outcome
        self.error = error

    async def evaluate(self, _window: WindowInfo) -> WindowGuardDecision:
        self.order.append("pre_guard")
        if self.error is not None:
            raise self.error
        return WindowGuardDecision(self.outcome, "unknown", "decision", "fingerprint")


class FakeCapture:
    def __init__(
        self,
        order: list[str],
        *,
        data: bytes = b"synthetic-pixels",
        error: Exception | None = None,
        after_capture: Callable[[], None] | None = None,
    ) -> None:
        self.order = order
        self.data = data
        self.error = error
        self.after_capture = after_capture
        self.frames: list[ImageFrame] = []

    async def capture(self, _window: WindowInfo) -> ImageFrame:
        self.order.append("capture")
        if self.error is not None:
            raise self.error
        frame = ImageFrame(bytearray(self.data), 100, 50, perceptual_hash=123)
        self.frames.append(frame)
        if self.after_capture is not None:
            self.after_capture()
        return frame


class ErrorChangeDetector:
    def compare(
        self,
        _window: WindowInfo,
        _frame: ImageFrame,
    ) -> FrameChangeAssessment:
        raise RuntimeError("change failure")

    def commit(self, _assessment: FrameChangeAssessment, *, sensitive: bool) -> None:
        del sensitive

    def reset(self) -> None:
        return None


class FakeOCR:
    def __init__(
        self,
        order: list[str],
        *,
        text: str = "def synthetic(): pass",
        box: Rect | None = _DEFAULT_OCR_BOX,
        error: Exception | None = None,
        block: bool = False,
    ) -> None:
        self.order = order
        self.text = text
        self.box = box
        self.error = error
        self.block = block
        self.results: list[OCRResult] = []
        self.closed = 0
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def extract(self, _frame: ImageFrame) -> OCRResult:
        self.order.append("ocr")
        self.started.set()
        try:
            if self.block:
                await asyncio.Event().wait()
            if self.error is not None:
                raise self.error
            result = OCRResult(spans=[OCRSpan(self.text, 0.9, self.box)])
            self.results.append(result)
            return result
        finally:
            self.finished.set()

    async def close(self) -> None:
        self.closed += 1


class FakeContentGuard:
    def __init__(
        self,
        order: list[str],
        *,
        outcome: GuardOutcome = GuardOutcome.allow,
        error: Exception | None = None,
    ) -> None:
        self.order = order
        self.outcome = outcome
        self.error = error

    def evaluate(self, _ocr: OCRResult) -> ContentGuardDecision:
        self.order.append("content_guard")
        if self.error is not None:
            raise self.error
        return ContentGuardDecision(
            self.outcome,
            reason_codes=("synthetic_sensitive",) if self.outcome is GuardOutcome.block else (),
        )


class RecordingContentGuard:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.delegate = OCRContentGuard()

    def evaluate(self, ocr: OCRResult) -> ContentGuardDecision:
        self.order.append("content_guard")
        return self.delegate.evaluate(ocr)


class FakeClassifier:
    def __init__(self, order: list[str], *, error: Exception | None = None) -> None:
        self.order = order
        self.error = error

    def classify(self, window: WindowInfo, ocr: OCRResult) -> SceneAnalysis:
        self.order.append("classify")
        if self.error is not None:
            raise self.error
        return LocalSceneClassifier().classify(window, ocr)


class FakeSanitizer:
    def __init__(
        self,
        order: list[str],
        *,
        error: Exception | None = None,
        after_sanitize: Callable[[], None] | None = None,
        block: bool = False,
        return_input: bool = False,
    ) -> None:
        self.order = order
        self.error = error
        self.after_sanitize = after_sanitize
        self.block = block
        self.return_input = return_input
        self.frames: list[ImageFrame] = []
        self.regions: list[tuple[Rect, ...]] = []
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.closed = 0

    async def sanitize(self, input_frame: ImageFrame, regions: tuple[Rect, ...]) -> ImageFrame:
        self.order.append("sanitize")
        self.regions.append(regions)
        self.started.set()
        try:
            if self.block:
                await asyncio.Event().wait()
            if self.error is not None:
                raise self.error
            frame = (
                input_frame
                if self.return_input
                else ImageFrame(bytearray(b"sanitized-only"), 50, 25)
            )
            self.frames.append(frame)
            if self.after_sanitize is not None:
                self.after_sanitize()
            return frame
        finally:
            self.finished.set()

    async def close(self) -> None:
        self.closed += 1


class FakeCloud:
    def __init__(
        self,
        order: list[str],
        *,
        error: Exception | None = None,
        analysis: CloudAnalysis | None = None,
        before_analyze: Callable[[], None] | None = None,
    ) -> None:
        self.order = order
        self.error = error
        self.analysis = analysis or CloudAnalysis(
            summary="safe scene, contact fake.person@example.test",
            confidence=5.0,
            category="reading",
        )
        self.before_analyze = before_analyze
        self.frames: list[ImageFrame] = []

    async def analyze(self, frame: ImageFrame, _local: SceneAnalysis) -> CloudAnalysis:
        self.order.append("cloud")
        self.frames.append(frame)
        if self.before_analyze is not None:
            self.before_analyze()
        if self.error is not None:
            raise self.error
        return self.analysis


class FlakyOCR(FakeOCR):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.attempts = 0

    async def extract(self, frame: ImageFrame) -> OCRResult:
        self.attempts += 1
        if self.attempts == 1:
            self.error = RuntimeError("synthetic transient OCR failure")
        try:
            return await super().extract(frame)
        finally:
            self.error = None


class FixedLimiter:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = 0

    def allow(self) -> bool:
        self.calls += 1
        return self.allowed


def _pipeline(
    *,
    enabled: EnabledFlag | None = None,
    cloud_enabled: EnabledFlag | None = None,
    order: list[str] | None = None,
    source: FakeWindowSource | None = None,
    guard: FakeWindowGuard | PreCaptureGuard | None = None,
    capture: FakeCapture | None = None,
    change: FrameChangeDetector | ErrorChangeDetector | None = None,
    ocr: FakeOCR | None = None,
    content: FakeContentGuard | RecordingContentGuard | None = None,
    classifier: FakeClassifier | None = None,
    sanitizer: ImageSanitizer | None = None,
    cloud: CloudVisionAnalyzer | None = None,
    limiter: FixedLimiter | None = None,
    max_frame_bytes: int = 1024,
    operation_timeout_seconds: float = 0.03,
) -> tuple[PerceptionPipeline, list[str], FakeCapture, FakeOCR]:
    calls = order if order is not None else []
    capture_impl = capture or FakeCapture(calls)
    ocr_impl = ocr or FakeOCR(calls)
    return (
        PerceptionPipeline(
            enabled=enabled or EnabledFlag(),
            cloud_enabled=cloud_enabled or EnabledFlag(),
            window_source=source or FakeWindowSource(calls),
            window_guard=guard or FakeWindowGuard(calls),
            capture=capture_impl,
            change_detector=change or FrameChangeDetector(),
            ocr=ocr_impl,
            content_guard=content or FakeContentGuard(calls),
            classifier=classifier or FakeClassifier(calls),
            redactor=TextRedactor(),
            sanitizer=sanitizer,
            cloud=cloud,
            cloud_limiter=limiter,
            config=PerceptionPipelineConfig(
                operation_timeout_seconds=operation_timeout_seconds,
                max_frame_bytes=max_frame_bytes,
                always_redact_regions=(Rect(0, 0, 5, 5),),
            ),
        ),
        calls,
        capture_impl,
        ocr_impl,
    )


def test_disabled_pipeline_does_not_poll_or_capture() -> None:
    async def scenario() -> None:
        flag = EnabledFlag(False)
        order: list[str] = []
        pipeline, _calls, _capture, _ocr = _pipeline(enabled=flag, order=order)
        result = await pipeline.observe()
        assert result.status is ObservationStatus.disabled
        assert order == []

        broken, calls, _capture, _ocr = _pipeline(enabled=RaisingEnabledFlag(), order=[])
        assert (await broken.observe()).status is ObservationStatus.disabled
        assert calls == []

    asyncio.run(scenario())


def test_pre_guard_block_and_error_never_capture() -> None:
    async def scenario() -> None:
        order: list[str] = []
        blocked, _calls, _capture, _ocr = _pipeline(
            order=order,
            guard=FakeWindowGuard(order, outcome=GuardOutcome.block),
        )
        result = await blocked.observe()
        assert result.status is ObservationStatus.blocked_before_capture
        assert result.context is not None and result.context.sensitive
        assert order == ["window", "pre_guard"]

        order = []
        failed, _calls, _capture, _ocr = _pipeline(
            order=order,
            guard=FakeWindowGuard(order, error=RuntimeError("raw title")),
        )
        result = await failed.observe()
        assert result.status is ObservationStatus.guard_error
        assert result.context is None
        assert "raw title" not in repr(result)
        assert order == ["window", "pre_guard"]

        order = []
        timeout, _calls, _capture, _ocr = _pipeline(
            order=order,
            source=BlockingWindowSource(order),
        )
        result = await timeout.observe()
        assert result.status is ObservationStatus.guard_error
        assert "capture" not in order

    asyncio.run(scenario())


def test_local_pipeline_order_and_cleanup() -> None:
    async def scenario() -> None:
        pipeline, order, capture, ocr = _pipeline()
        result = await pipeline.observe()
        assert result.status is ObservationStatus.analyzed_local
        assert result.context is not None
        assert result.context.category == "coding"
        assert result.context.summary == "用户正在使用编程或终端工具。"
        assert order == ["window", "pre_guard", "capture", "ocr", "content_guard", "classify"]
        assert capture.frames[0].data == bytearray()
        assert ocr.results[0].spans == []

    asyncio.run(scenario())


def test_unchanged_frame_skips_ocr_and_still_wipes_capture() -> None:
    async def scenario() -> None:
        detector = FrameChangeDetector()
        pipeline, _order, capture, ocr = _pipeline(change=detector)
        assert (await pipeline.observe()).status is ObservationStatus.analyzed_local
        assert (await pipeline.observe()).status is ObservationStatus.unchanged
        assert len(ocr.results) == 1
        assert all(frame.data == bytearray() for frame in capture.frames)

    asyncio.run(scenario())


def test_sensitive_and_failed_frames_cannot_be_bypassed_as_unchanged() -> None:
    async def scenario() -> None:
        detector = FrameChangeDetector()
        order: list[str] = []
        sensitive_ocr = FakeOCR(order, text="fake.person@example.test")
        sensitive, _calls, _capture, _ocr = _pipeline(
            order=order,
            change=detector,
            ocr=sensitive_ocr,
            content=RecordingContentGuard(order),
        )

        first = await sensitive.observe()
        second = await sensitive.observe()
        assert first.status is ObservationStatus.blocked_after_ocr
        assert second.status is ObservationStatus.unchanged
        assert second.context is not None and second.context.sensitive
        assert second.reason_code == "sticky_sensitive_context"
        assert len(sensitive_ocr.results) == 1

        retry_order: list[str] = []
        flaky = FlakyOCR(retry_order)
        retry, _calls, _capture, _ocr = _pipeline(
            order=retry_order,
            change=FrameChangeDetector(),
            ocr=flaky,
        )
        assert (await retry.observe()).status is ObservationStatus.ocr_error
        assert (await retry.observe()).status is ObservationStatus.analyzed_local
        assert flaky.attempts == 2

        cloud_order: list[str] = []
        sanitizer = FakeSanitizer(cloud_order)
        cloud = FakeCloud(cloud_order, error=RuntimeError("synthetic transient cloud failure"))
        cloud_retry, _calls, _capture, _ocr = _pipeline(
            order=cloud_order,
            sanitizer=sanitizer,
            cloud=cloud,
        )
        assert (await cloud_retry.observe()).status is ObservationStatus.cloud_error
        cloud.error = None
        assert (await cloud_retry.observe()).status is ObservationStatus.analyzed_cloud
        assert len(cloud.frames) == 2
        assert all(frame.data == bytearray() for frame in cloud.frames)

    asyncio.run(scenario())


def test_sensitive_ocr_never_reaches_classifier_or_cloud() -> None:
    async def scenario() -> None:
        sentinel = "fake.person@example.test"
        order: list[str] = []
        ocr = FakeOCR(order, text=sentinel)
        pipeline, _calls, capture, _ocr = _pipeline(
            order=order,
            ocr=ocr,
            content=RecordingContentGuard(order),
            classifier=FakeClassifier(order),
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.blocked_after_ocr
        assert result.context is not None and result.context.sensitive
        assert "classify" not in order and "cloud" not in order
        assert sentinel not in repr(result)
        assert capture.frames[0].data == bytearray()
        assert ocr.results[0].spans == []

    asyncio.run(scenario())


def test_cloud_path_uses_sanitized_frame_redacts_summary_and_cleans_both_frames() -> None:
    async def scenario() -> None:
        order: list[str] = []
        capture = FakeCapture(order)
        ocr = FakeOCR(order)
        sanitizer = FakeSanitizer(order)

        def assert_raw_inputs_already_wiped() -> None:
            assert capture.frames[0].data == bytearray()
            assert ocr.results[0].spans == []

        cloud = FakeCloud(order, before_analyze=assert_raw_inputs_already_wiped)
        limiter = FixedLimiter(True)
        pipeline, _calls, _capture, _ocr = _pipeline(
            order=order,
            capture=capture,
            ocr=ocr,
            sanitizer=sanitizer,
            cloud=cloud,
            limiter=limiter,
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.analyzed_cloud
        assert result.context is not None
        assert result.context.category == "reading"
        assert result.context.confidence == 1.0
        assert "fake.person" not in result.context.summary
        assert "[EMAIL]" in result.context.summary
        assert order[-3:] == ["classify", "sanitize", "cloud"]
        assert sanitizer.regions == [(Rect(0, 0, 5, 5), Rect(1, 1, 10, 5))]
        assert cloud.frames[0] is sanitizer.frames[0]
        assert sanitizer.frames[0].data == bytearray()
        assert capture.frames[0].data == bytearray()
        assert ocr.results[0].spans == []

    asyncio.run(scenario())


def test_cloud_request_pixels_mask_every_ocr_box_with_real_sanitizer() -> None:
    image_module: Any = pytest.importorskip("PIL.Image")
    draw_module: Any = pytest.importorskip("PIL.ImageDraw")
    image = image_module.new("RGB", (100, 50), "white")
    draw_module.Draw(image).rectangle((10, 10, 30, 25), fill=(255, 0, 0))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")

    class PixelInspectingCloud:
        def __init__(self) -> None:
            self.calls = 0
            self.inside: tuple[int, int, int] | None = None
            self.outside: tuple[int, int, int] | None = None

        async def analyze(self, frame: ImageFrame, _local: SceneAnalysis) -> CloudAnalysis:
            self.calls += 1
            with image_module.open(io.BytesIO(frame.data)) as sanitized:
                rgb = sanitized.convert("RGB")
                self.inside = rgb.getpixel((15, 15))
                self.outside = rgb.getpixel((40, 15))
            return CloudAnalysis("ordinary", 0.8, "coding")

    async def scenario() -> None:
        order: list[str] = []
        cloud = PixelInspectingCloud()
        pipeline, _calls, _capture, _ocr = _pipeline(
            order=order,
            capture=FakeCapture(order, data=encoded.getvalue()),
            ocr=FakeOCR(order, text="VISIBLE OCR", box=Rect(10, 10, 20, 15)),
            sanitizer=PillowImageSanitizer(),
            cloud=cloud,
            operation_timeout_seconds=1.0,
        )

        result = await pipeline.observe()
        assert result.status is ObservationStatus.analyzed_cloud
        assert cloud.calls == 1
        assert cloud.inside == (0, 0, 0)
        assert cloud.outside == (255, 255, 255)
        await pipeline.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("box", [None, Rect(-1, 1, 5, 5), Rect(95, 1, 10, 5)])
def test_unreliable_ocr_box_falls_back_local_with_zero_cloud_calls(box: Rect | None) -> None:
    async def scenario() -> None:
        order: list[str] = []
        sanitizer = FakeSanitizer(order)
        cloud = FakeCloud(order)
        limiter = FixedLimiter(True)
        pipeline, _calls, _capture, _ocr = _pipeline(
            order=order,
            ocr=FakeOCR(order, text="VISIBLE OCR", box=box),
            sanitizer=sanitizer,
            cloud=cloud,
            limiter=limiter,
        )

        result = await pipeline.observe()
        assert result.status is ObservationStatus.analyzed_local
        assert result.context is not None
        assert result.reason_code == "cloud_ocr_redaction_unavailable"
        assert limiter.calls == 0
        assert sanitizer.frames == []
        assert cloud.frames == []

    asyncio.run(scenario())


def test_cloud_rate_limit_returns_local_result_without_sanitization() -> None:
    async def scenario() -> None:
        order: list[str] = []
        sanitizer = FakeSanitizer(order)
        cloud = FakeCloud(order)
        limiter = FixedLimiter(False)
        pipeline, _calls, _capture, _ocr = _pipeline(
            order=order,
            sanitizer=sanitizer,
            cloud=cloud,
            limiter=limiter,
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.cloud_rate_limited
        assert result.context is not None and result.context.category == "coding"
        assert limiter.calls == 1
        assert sanitizer.frames == [] and cloud.frames == []

    asyncio.run(scenario())


def test_disabled_or_broken_cloud_feature_never_sanitizes_or_calls_network() -> None:
    async def scenario() -> None:
        for flag in (EnabledFlag(False), RaisingEnabledFlag()):
            order: list[str] = []
            sanitizer = FakeSanitizer(order)
            cloud = FakeCloud(order)
            pipeline, _calls, _capture, _ocr = _pipeline(
                order=order,
                cloud_enabled=flag,
                sanitizer=sanitizer,
                cloud=cloud,
            )
            result = await pipeline.observe()
            assert result.status is ObservationStatus.analyzed_local
            assert sanitizer.frames == []
            assert cloud.frames == []

    asyncio.run(scenario())


def test_cloud_gate_defaults_closed_and_is_rechecked_after_sanitization() -> None:
    async def scenario() -> None:
        order: list[str] = []
        sanitizer = FakeSanitizer(order)
        cloud = FakeCloud(order)
        pipeline = PerceptionPipeline(
            enabled=EnabledFlag(),
            window_source=FakeWindowSource(order),
            window_guard=FakeWindowGuard(order),
            capture=FakeCapture(order),
            change_detector=FrameChangeDetector(),
            ocr=FakeOCR(order),
            content_guard=FakeContentGuard(order),
            classifier=FakeClassifier(order),
            redactor=TextRedactor(),
            sanitizer=sanitizer,
            cloud=cloud,
        )
        assert (await pipeline.observe()).status is ObservationStatus.analyzed_local
        assert sanitizer.frames == [] and cloud.frames == []
        await pipeline.close()

        order = []
        cloud_flag = EnabledFlag()
        sanitizer = FakeSanitizer(
            order,
            after_sanitize=lambda: setattr(cloud_flag, "enabled", False),
        )
        cloud = FakeCloud(order)
        pipeline, _calls, capture, _ocr = _pipeline(
            order=order,
            cloud_enabled=cloud_flag,
            sanitizer=sanitizer,
            cloud=cloud,
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.analyzed_local
        assert cloud.frames == []
        assert capture.frames[0].data == bytearray()
        assert sanitizer.frames[0].data == bytearray()

        order = []
        identity = FakeSanitizer(order, return_input=True)
        cloud = FakeCloud(order)
        pipeline, _calls, capture, _ocr = _pipeline(
            order=order,
            sanitizer=identity,
            cloud=cloud,
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.analyzed_local
        assert result.context is not None
        assert result.reason_code == "cloud_redaction_failed"
        assert cloud.frames == []
        assert capture.frames[0].data == bytearray()

    asyncio.run(scenario())


def test_cloud_category_is_allowlisted_and_nonfinite_confidence_fails_closed() -> None:
    async def scenario() -> None:
        order: list[str] = []
        sanitizer = FakeSanitizer(order)
        unsafe_category = FakeCloud(
            order,
            analysis=CloudAnalysis("generic", 0.5, "fake.person@example.test"),
        )
        pipeline, _calls, _capture, _ocr = _pipeline(
            order=order,
            sanitizer=sanitizer,
            cloud=unsafe_category,
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.analyzed_cloud
        assert result.context is not None and result.context.category == "coding"

        order = []
        sanitizer = FakeSanitizer(order)
        invalid_confidence = FakeCloud(
            order,
            analysis=CloudAnalysis("generic", float("nan"), "reading"),
        )
        pipeline, _calls, _capture, _ocr = _pipeline(
            order=order,
            sanitizer=sanitizer,
            cloud=invalid_confidence,
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.cloud_error
        assert result.context is None
        assert sanitizer.frames[0].data == bytearray()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("capture", ObservationStatus.capture_error),
        ("large", ObservationStatus.capture_error),
        ("change", ObservationStatus.processing_error),
        ("ocr", ObservationStatus.ocr_error),
        ("content", ObservationStatus.guard_error),
        ("classify", ObservationStatus.processing_error),
        ("sanitize", ObservationStatus.analyzed_local),
        ("cloud", ObservationStatus.cloud_error),
    ],
)
def test_every_stage_failure_is_fail_closed_and_cleans_raw_data(
    stage: str, expected: ObservationStatus
) -> None:
    async def scenario() -> None:
        order: list[str] = []
        capture = FakeCapture(
            order,
            data=b"x" * 20 if stage == "large" else b"synthetic-pixels",
            error=RuntimeError("private capture") if stage == "capture" else None,
        )
        ocr = FakeOCR(order, error=RuntimeError("private ocr") if stage == "ocr" else None)
        sanitizer = (
            FakeSanitizer(
                order,
                error=RuntimeError("private sanitize") if stage == "sanitize" else None,
            )
            if stage in {"sanitize", "cloud"}
            else None
        )
        cloud = (
            FakeCloud(order, error=RuntimeError("private cloud") if stage == "cloud" else None)
            if sanitizer is not None
            else None
        )
        pipeline, _calls, _capture, _ocr = _pipeline(
            order=order,
            capture=capture,
            change=ErrorChangeDetector() if stage == "change" else FrameChangeDetector(),
            ocr=ocr,
            content=FakeContentGuard(
                order, error=RuntimeError("private content") if stage == "content" else None
            ),
            classifier=FakeClassifier(
                order, error=RuntimeError("private classify") if stage == "classify" else None
            ),
            sanitizer=sanitizer,
            cloud=cloud,
            max_frame_bytes=10 if stage == "large" else 1024,
        )
        result = await pipeline.observe()
        assert result.status is expected
        if stage == "sanitize":
            assert result.context is not None
            assert result.reason_code == "cloud_redaction_failed"
            assert cloud is not None and cloud.frames == []
        else:
            assert result.context is None
        assert "private" not in repr(result)
        assert all(frame.data == bytearray() for frame in capture.frames)
        assert all(result.spans == [] for result in ocr.results)
        if sanitizer is not None:
            assert all(frame.data == bytearray() for frame in sanitizer.frames)

    asyncio.run(scenario())


def test_disable_during_capture_or_after_sanitize_stops_downstream_work() -> None:
    async def scenario() -> None:
        order: list[str] = []
        flag = EnabledFlag()
        capture = FakeCapture(order, after_capture=lambda: setattr(flag, "enabled", False))
        pipeline, _calls, _capture, ocr = _pipeline(
            enabled=flag,
            order=order,
            capture=capture,
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.disabled
        assert ocr.results == []
        assert capture.frames[0].data == bytearray()

        order = []
        flag = EnabledFlag()
        sanitizer = FakeSanitizer(order, after_sanitize=lambda: setattr(flag, "enabled", False))
        cloud = FakeCloud(order)
        pipeline, _calls, _capture, _ocr = _pipeline(
            enabled=flag,
            order=order,
            sanitizer=sanitizer,
            cloud=cloud,
        )
        result = await pipeline.observe()
        assert result.status is ObservationStatus.disabled
        assert cloud.frames == []
        assert sanitizer.frames[0].data == bytearray()

    asyncio.run(scenario())


def test_cancellation_propagates_and_cleans_frame() -> None:
    async def scenario() -> None:
        order: list[str] = []
        ocr = FakeOCR(order, block=True)
        pipeline, _calls, capture, _ocr = _pipeline(order=order, ocr=ocr)
        task = asyncio.create_task(pipeline.observe())
        await ocr.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert capture.frames[0].data == bytearray()

    asyncio.run(scenario())


def test_vision_disable_and_close_cancel_and_await_inflight_observations() -> None:
    async def scenario() -> None:
        order: list[str] = []
        ocr = FakeOCR(order, block=True)
        pipeline, _calls, capture, _ocr = _pipeline(order=order, ocr=ocr)
        observing = asyncio.create_task(pipeline.observe())
        await ocr.started.wait()

        await pipeline.set_vision_enabled(False)
        result = await observing
        assert result.status is ObservationStatus.disabled
        assert ocr.finished.is_set()
        assert capture.frames[0].data == bytearray()

        order = []
        sanitizer = FakeSanitizer(order, block=True)
        cloud = FakeCloud(order)
        pipeline, _calls, capture, ocr = _pipeline(
            order=order,
            sanitizer=sanitizer,
            cloud=cloud,
        )
        observing = asyncio.create_task(pipeline.observe())
        await sanitizer.started.wait()

        await pipeline.close()
        result = await observing
        assert result.status is ObservationStatus.disabled
        assert sanitizer.finished.is_set()
        assert sanitizer.closed == 1
        assert ocr.closed == 1
        assert cloud.frames == []
        assert capture.frames[0].data == bytearray()

    asyncio.run(scenario())


def test_same_loop_reenable_supersedes_pending_feature_disable() -> None:
    async def scenario() -> None:
        pipeline, _order, _capture, _ocr = _pipeline()
        assert (await pipeline.observe()).status is ObservationStatus.analyzed_local

        pipeline.notify_vision_enabled(False)
        pipeline.notify_vision_enabled(True)
        await asyncio.sleep(0)

        assert (await pipeline.observe()).status is ObservationStatus.unchanged
        await pipeline.close()

    asyncio.run(scenario())


def test_close_is_idempotent_and_disables_future_observation() -> None:
    async def scenario() -> None:
        pipeline, _order, _capture, ocr = _pipeline()
        await pipeline.close()
        await pipeline.close()
        assert ocr.closed == 1
        assert (await pipeline.observe()).status is ObservationStatus.disabled

    asyncio.run(scenario())


def test_pipeline_configuration_rejects_unsafe_combinations() -> None:
    with pytest.raises(ValueError):
        PerceptionPipelineConfig(operation_timeout_seconds=0)
    order: list[str] = []
    with pytest.raises(ValueError, match="同时"):
        PerceptionPipeline(
            enabled=EnabledFlag(),
            window_source=FakeWindowSource(order),
            window_guard=FakeWindowGuard(order),
            capture=FakeCapture(order),
            change_detector=FrameChangeDetector(),
            ocr=FakeOCR(order),
            content_guard=FakeContentGuard(order),
            classifier=FakeClassifier(order),
            redactor=TextRedactor(),
            sanitizer=FakeSanitizer(order),
            cloud=None,
        )
