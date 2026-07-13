"""Composable boundaries for platform and provider-specific perception work."""

from __future__ import annotations

from typing import Protocol

from app.perception.models import (
    CloudAnalysis,
    ContentGuardDecision,
    ImageFrame,
    OCRResult,
    Rect,
    SceneAnalysis,
    WindowGuardDecision,
    WindowInfo,
)


class ActiveWindowSource(Protocol):
    async def active_window(self) -> WindowInfo: ...


class WindowCapture(Protocol):
    async def capture(self, window: WindowInfo) -> ImageFrame: ...


class WindowGuard(Protocol):
    async def evaluate(self, window: WindowInfo) -> WindowGuardDecision: ...


class OCRProvider(Protocol):
    async def extract(self, frame: ImageFrame) -> OCRResult: ...

    async def close(self) -> None: ...


class ContentGuard(Protocol):
    def evaluate(self, ocr: OCRResult) -> ContentGuardDecision: ...


class ChangeDetector(Protocol):
    def should_analyze(self, window: WindowInfo, frame: ImageFrame) -> bool: ...


class SceneClassifier(Protocol):
    def classify(self, window: WindowInfo, ocr: OCRResult) -> SceneAnalysis: ...


class ImageSanitizer(Protocol):
    async def sanitize(self, frame: ImageFrame, regions: tuple[Rect, ...]) -> ImageFrame: ...


class CloudVisionAnalyzer(Protocol):
    async def analyze(self, frame: ImageFrame, local: SceneAnalysis) -> CloudAnalysis: ...


class RateLimiter(Protocol):
    def allow(self) -> bool: ...
