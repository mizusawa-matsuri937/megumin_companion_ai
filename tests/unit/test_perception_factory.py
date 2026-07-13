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
        self.listeners: set[Callable[[FeatureState], None]] = set()

    def get_feature(self, name: FeatureName) -> FeatureState:
        return FeatureState(name=name, enabled=self.values[name])

    def subscribe(self, listener: Callable[[FeatureState], None]) -> Callable[[], None]:
        self.listeners.add(listener)

        def unsubscribe() -> None:
            self.listeners.discard(listener)

        return unsubscribe

    def set(self, name: FeatureName, enabled: bool) -> None:
        self.values[name] = enabled
        state = FeatureState(name=name, enabled=enabled)
        for listener in tuple(self.listeners):
            listener(state)


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
        return OCRResult([OCRSpan("def synthetic(): pass", 0.9, Rect(1, 1, 10, 5))])

    async def close(self) -> None:
        return None


class BlockingOCR(OCR):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def extract(self, _frame: ImageFrame) -> OCRResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.finished.set()
        raise AssertionError("unreachable")


class Sanitizer:
    def __init__(self) -> None:
        self.calls = 0
        self.regions: list[tuple[Rect, ...]] = []

    async def sanitize(self, _frame: ImageFrame, regions: tuple[Rect, ...]) -> ImageFrame:
        self.calls += 1
        self.regions.append(regions)
        return ImageFrame(bytearray(b"sanitized"), 100, 50)

    async def close(self) -> None:
        return None


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

        flags.set(FeatureName.vision, True)
        assert (await pipeline.observe()).status is ObservationStatus.analyzed_local
        assert sanitizer.calls == 0 and cloud.calls == 0

        flags.set(FeatureName.cloud_vision, True)
        assert (await pipeline.observe()).status is ObservationStatus.analyzed_cloud
        assert sanitizer.calls == 1 and cloud.calls == 1
        assert sanitizer.regions == [(Rect(1, 1, 10, 5),)]
        await pipeline.close()

    asyncio.run(scenario())


def test_factory_feature_listener_awaits_inflight_vision_shutdown() -> None:
    async def scenario() -> None:
        flags = Flags()
        flags.values[FeatureName.vision] = True
        ocr = BlockingOCR()
        pipeline = build_perception_pipeline(
            Settings(),
            flags,
            window_source=WindowSource(),
            capture=Capture(),
            ocr=ocr,
        )
        observing = asyncio.create_task(pipeline.observe())
        await ocr.started.wait()

        await asyncio.to_thread(flags.set, FeatureName.vision, False)
        result = await observing
        assert result.status is ObservationStatus.disabled
        assert ocr.finished.is_set()

        await pipeline.close()
        assert flags.listeners == set()

    asyncio.run(scenario())
