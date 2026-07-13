"""Optional OCR and image-sanitizer adapter contract tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from app.perception.image_processing import PillowImageSanitizer, PillowSanitizerConfig
from app.perception.models import ImageFrame, OCRResult, OCRSpan, PerceptionError, Rect
from app.perception.ocr import RapidOCRProvider


class ColumnOutput:
    txts = ["hello", "world"]
    scores = [1.2, "bad"]
    boxes = [
        [[1, 2], [11, 2], [11, 8], [1, 8]],
        "invalid-box",
    ]


class FakeEngine:
    def __init__(self, output: Any) -> None:
        self.output = output
        self.calls: list[bytes] = []

    def __call__(self, image: bytes) -> Any:
        self.calls.append(image)
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def _frame(data: bytes = b"synthetic-image") -> ImageFrame:
    return ImageFrame(data=bytearray(data), width=100, height=50)


async def _wait_thread_event(event: threading.Event) -> None:
    async with asyncio.timeout(1):
        while not event.is_set():
            await asyncio.sleep(0)


class BlockingRapidOCR(RapidOCRProvider):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(engine_factory=lambda: None)
        self.fail = fail
        self.started = threading.Event()
        self.release = threading.Event()
        self.owned_input: bytearray | None = None
        self.produced: OCRResult | None = None

    def _extract_sync(self, image_buffer: bytearray) -> OCRResult:
        self.owned_input = image_buffer
        self.started.set()
        assert self.release.wait(timeout=2)
        if self.fail:
            raise RuntimeError("synthetic private worker failure")
        self.produced = OCRResult([OCRSpan("synthetic result", 1.0)])
        return self.produced


def test_rapidocr_adapter_parses_current_and_legacy_shapes_and_clamps_values() -> None:
    async def scenario() -> None:
        engine = FakeEngine(ColumnOutput())
        provider = RapidOCRProvider(engine_factory=lambda: engine)
        current = await provider.extract(_frame())
        assert [span.text for span in current.spans] == ["hello", "world"]
        assert [span.confidence for span in current.spans] == [1.0, 0.0]
        assert current.spans[0].box == Rect(1, 2, 10, 6)
        assert current.spans[1].box is None
        assert engine.calls == [b"synthetic-image"]
        await provider.close()

        legacy_engine = FakeEngine(([[[[0, 0], [5, 0], [5, 5], [0, 5]], "legacy", 0.75]], 0.01))
        legacy = RapidOCRProvider(engine_factory=lambda: legacy_engine, max_span_chars=3)
        result = await legacy.extract(_frame())
        assert result.spans[0].text == "leg"
        assert result.spans[0].confidence == 0.75
        assert result.spans[0].box == Rect(0, 0, 5, 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("output", [None, "bad", [["short"]]])
def test_rapidocr_adapter_handles_empty_or_invalid_output(output: Any) -> None:
    async def scenario() -> None:
        provider = RapidOCRProvider(engine_factory=lambda: FakeEngine(output))
        if output is None:
            assert (await provider.extract(_frame())).spans == []
        else:
            with pytest.raises(PerceptionError):
                await provider.extract(_frame())

    asyncio.run(scenario())


def test_rapidocr_is_lazy_and_reports_missing_optional_dependency() -> None:
    async def scenario() -> None:
        provider = RapidOCRProvider()
        with (
            patch("app.perception.ocr.importlib.import_module", side_effect=ImportError("missing")),
            pytest.raises(PerceptionError, match="rapidocr") as caught,
        ):
            await provider.extract(_frame())
        assert "synthetic-image" not in str(caught.value)

        failing = RapidOCRProvider(engine_factory=lambda: FakeEngine(RuntimeError("private")))
        with pytest.raises(PerceptionError, match="本地 OCR") as failure:
            await failing.extract(_frame())
        assert "private" not in str(failure.value)

    asyncio.run(scenario())


def test_rapidocr_limits_are_validated() -> None:
    with pytest.raises(ValueError):
        RapidOCRProvider(max_spans=0)


def test_rapidocr_cancellation_and_timeout_drain_worker_and_discard_result() -> None:
    async def scenario() -> None:
        provider = BlockingRapidOCR()
        extracting = asyncio.create_task(provider.extract(_frame(b"cancel-private")))
        await _wait_thread_event(provider.started)
        extracting.cancel()
        closing = asyncio.create_task(provider.close())
        await asyncio.sleep(0.01)
        assert not extracting.done()
        assert not closing.done()
        closing.cancel()
        await asyncio.sleep(0.01)
        assert not closing.done()

        provider.release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(asyncio.CancelledError):
            await extracting
        assert provider.owned_input == bytearray()
        assert provider.produced is not None and provider.produced.spans == []
        await provider.close()

        timeout_provider = BlockingRapidOCR()

        async def release_later() -> None:
            await _wait_thread_event(timeout_provider.started)
            await asyncio.sleep(0.03)
            timeout_provider.release.set()

        releaser = asyncio.create_task(release_later())
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await timeout_provider.extract(_frame(b"timeout-private"))
        await releaser
        assert timeout_provider.owned_input == bytearray()
        assert timeout_provider.produced is not None and timeout_provider.produced.spans == []
        await timeout_provider.close()

        failing_provider = BlockingRapidOCR(fail=True)
        failed = asyncio.create_task(failing_provider.extract(_frame(b"failure-private")))
        await _wait_thread_event(failing_provider.started)
        failed.cancel()
        failing_provider.release.set()
        with pytest.raises(asyncio.CancelledError):
            await failed
        assert failing_provider.owned_input == bytearray()
        await failing_provider.close()

    asyncio.run(scenario())


def test_rapidocr_close_waits_for_owned_worker() -> None:
    async def scenario() -> None:
        provider = BlockingRapidOCR()
        extracting = asyncio.create_task(provider.extract(_frame()))
        await _wait_thread_event(provider.started)
        closing = asyncio.create_task(provider.close())
        await asyncio.sleep(0.01)
        assert not closing.done()

        provider.release.set()
        await closing
        assert provider.owned_input == bytearray()
        result = await extracting
        assert result.text == "synthetic result"
        with pytest.raises(PerceptionError, match="已关闭"):
            await provider.extract(_frame())

    asyncio.run(scenario())


class FakeConvertedImage:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.rectangles: list[tuple[tuple[int, int, int, int], tuple[int, int, int]]] = []

    def thumbnail(self, size: tuple[int, int]) -> None:
        ratio = min(size[0] / self.width, size[1] / self.height, 1.0)
        self.width = round(self.width * ratio)
        self.height = round(self.height * ratio)

    def save(self, output: Any, *, format: str, optimize: bool) -> None:
        assert format == "PNG" and optimize
        output.write(b"sanitized-png")


class FakeOpenedImage:
    def __init__(self, width: int = 100, height: int = 50) -> None:
        self.width = width
        self.height = height
        self.converted = FakeConvertedImage(width, height)
        self.loaded = False

    def __enter__(self) -> FakeOpenedImage:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def load(self) -> None:
        self.loaded = True

    def convert(self, mode: str) -> FakeConvertedImage:
        assert mode == "RGB"
        return self.converted


class FakeImageModule:
    def __init__(self, opened: FakeOpenedImage) -> None:
        self.opened = opened

    def open(self, _stream: Any) -> FakeOpenedImage:
        return self.opened


class FakeDrawer:
    def __init__(self, image: FakeConvertedImage) -> None:
        self.image = image

    def rectangle(
        self,
        coordinates: tuple[int, int, int, int],
        *,
        fill: tuple[int, int, int],
    ) -> None:
        self.image.rectangles.append((coordinates, fill))


class FakeDrawModule:
    @staticmethod
    def Draw(image: FakeConvertedImage) -> FakeDrawer:  # noqa: N802
        return FakeDrawer(image)


class BlockingPillowSanitizer(PillowImageSanitizer):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.owned_input: bytearray | None = None
        self.produced: ImageFrame | None = None

    def _sanitize_sync(
        self,
        image_buffer: bytearray,
        regions: tuple[Rect, ...],
    ) -> ImageFrame:
        del regions
        self.owned_input = image_buffer
        self.started.set()
        assert self.release.wait(timeout=2)
        self.produced = ImageFrame(bytearray(b"sanitized"), 10, 10)
        return self.produced


def test_pillow_sanitizer_resizes_masks_and_returns_new_frame() -> None:
    async def scenario() -> None:
        opened = FakeOpenedImage()

        def load_module(name: str) -> Any:
            return FakeImageModule(opened) if name == "PIL.Image" else FakeDrawModule

        sanitizer = PillowImageSanitizer(PillowSanitizerConfig(max_dimension=50))
        original = _frame()
        with patch(
            "app.perception.image_processing.importlib.import_module", side_effect=load_module
        ):
            result = await sanitizer.sanitize(original, (Rect(10, 10, 20, 10),))
        assert result.data == bytearray(b"sanitized-png")
        assert (result.width, result.height) == (50, 25)
        assert opened.loaded
        assert opened.converted.rectangles == [((5, 5, 15, 10), (0, 0, 0))]
        assert original.data == bytearray(b"synthetic-image")

    asyncio.run(scenario())


def test_pillow_sanitizer_fails_closed_for_limits_dependency_and_decode_error() -> None:
    async def scenario() -> None:
        too_large = PillowImageSanitizer(PillowSanitizerConfig(max_input_bytes=2))
        with pytest.raises(PerceptionError, match="大小"):
            await too_large.sanitize(_frame(), ())

        sanitizer = PillowImageSanitizer()
        with (
            patch(
                "app.perception.image_processing.importlib.import_module",
                side_effect=ImportError("missing"),
            ),
            pytest.raises(PerceptionError, match="Pillow"),
        ):
            await sanitizer.sanitize(_frame(), ())

        huge = FakeOpenedImage(width=10_000, height=10_000)

        def load_huge(name: str) -> Any:
            return FakeImageModule(huge) if name == "PIL.Image" else FakeDrawModule

        with (
            patch("app.perception.image_processing.importlib.import_module", side_effect=load_huge),
            pytest.raises(PerceptionError, match="像素"),
        ):
            await sanitizer.sanitize(_frame(), ())

    asyncio.run(scenario())
    with pytest.raises(ValueError):
        PillowSanitizerConfig(max_dimension=0)


def test_pillow_cancellation_discards_output_and_close_waits_for_worker() -> None:
    async def scenario() -> None:
        sanitizer = BlockingPillowSanitizer()
        sanitizing = asyncio.create_task(sanitizer.sanitize(_frame(b"private-pixels"), ()))
        await _wait_thread_event(sanitizer.started)
        sanitizing.cancel()
        closing = asyncio.create_task(sanitizer.close())
        await asyncio.sleep(0.01)
        assert not sanitizing.done()
        assert not closing.done()

        sanitizer.release.set()
        await closing
        assert sanitizer.owned_input == bytearray()
        assert sanitizer.produced is not None and sanitizer.produced.data == bytearray()
        with pytest.raises(asyncio.CancelledError):
            await sanitizing
        with pytest.raises(PerceptionError, match="已关闭"):
            await sanitizer.sanitize(_frame(), ())

    asyncio.run(scenario())


def test_optional_rapidocr_real_inference_on_synthetic_image(tmp_path: Path) -> None:
    rapidocr = pytest.importorskip("rapidocr", reason="optional rapidocr extra is not installed")
    image_module = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    font_module = pytest.importorskip("PIL.ImageFont")
    assert rapidocr is not None

    image = image_module.new("RGB", (640, 160), "white")
    draw = draw_module.Draw(image)
    font_paths = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    font_path = next((path for path in font_paths if path.is_file()), None)
    if font_path is None:
        pytest.skip("no deterministic test font is available")
    font = font_module.truetype(str(font_path), 64)
    draw.text((30, 35), "HELLO 1234", fill="black", font=font)
    image_path = tmp_path / "synthetic.png"
    image.save(image_path)

    async def scenario() -> None:
        provider = RapidOCRProvider()
        result = await provider.extract(
            ImageFrame(
                data=bytearray(image_path.read_bytes()),
                width=640,
                height=160,
            )
        )
        await provider.close()
        combined = result.text.replace(" ", "").upper()
        assert "HELLO" in combined
        assert "1234" in combined

    asyncio.run(scenario())
