"""Shared feature gates and injected platform adapter composition."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.config import Settings
from app.perception.factory import build_perception_pipeline
from app.perception.models import (
    CloudAnalysis,
    ImageFrame,
    ObservationStatus,
    OCRResult,
    OCRSpan,
    Rect,
    SceneAnalysis,
    WindowInfo,
)
from app.schemas import FeatureName, FeatureState


class Flags:
    def __init__(self) -> None:
        self.values = {name: False for name in FeatureName}

    def get_feature(self, name: FeatureName) -> FeatureState:
        return FeatureState(name=name, enabled=self.values[name])

    def subscribe(self, _listener: Callable[[FeatureState], None]) -> Callable[[], None]:
        return lambda: None


class WindowSource:
    def __init__(self) -> None:
        self.calls = 0

    async def active_window(self) -> WindowInfo:
        self.calls += 1
        return WindowInfo("synthetic", "ordinary", "Code", Rect(0, 0, 100, 50))


class Capture:
    def __init__(self) -> None:
        self.calls = 0

    async def capture(self, _window: WindowInfo) -> ImageFrame:
        self.calls += 1
        return ImageFrame(bytearray(f"synthetic-{self.calls}".encode()), 100, 50)


class OCR:
    async def extract(self, _frame: ImageFrame) -> OCRResult:
        return OCRResult([OCRSpan("def synthetic(): pass", 0.9)])

    async def close(self) -> None:
        return None


class Sanitizer:
    def __init__(self) -> None:
        self.calls = 0

    async def sanitize(self, _frame: ImageFrame, _regions: tuple[Rect, ...]) -> ImageFrame:
        self.calls += 1
        return ImageFrame(bytearray(b"sanitized"), 100, 50)


class Cloud:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, _frame: ImageFrame, _local: SceneAnalysis) -> CloudAnalysis:
        self.calls += 1
        return CloudAnalysis("普通开发场景", 0.9, "coding")


def test_factory_uses_independent_vision_and_cloud_feature_gates() -> None:
    async def scenario() -> None:
        flags = Flags()
        source = WindowSource()
        sanitizer = Sanitizer()
        cloud = Cloud()
        pipeline = build_perception_pipeline(
            Settings(),
            flags,
            window_source=source,
            capture=Capture(),
            ocr=OCR(),
            sanitizer=sanitizer,
            cloud=cloud,
        )

        assert (await pipeline.observe()).status is ObservationStatus.disabled
        assert source.calls == 0

        flags.values[FeatureName.vision] = True
        assert (await pipeline.observe()).status is ObservationStatus.analyzed_local
        assert sanitizer.calls == 0 and cloud.calls == 0

        flags.values[FeatureName.cloud_vision] = True
        assert (await pipeline.observe()).status is ObservationStatus.analyzed_cloud
        assert sanitizer.calls == 1 and cloud.calls == 1
        await pipeline.close()

    asyncio.run(scenario())
