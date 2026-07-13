"""Optional, lazily imported RapidOCR adapter."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Sequence
from typing import Any

from app.perception.models import ImageFrame, OCRResult, OCRSpan, PerceptionError, Rect
from app.perception.thread_jobs import (
    await_owned_job,
    drain_owned_jobs,
    wipe_buffer,
)


class RapidOCRProvider:
    """Use RapidOCR when its optional runtime dependencies are installed.

    The default application can import and start without RapidOCR.  Engine
    construction occurs only on the first explicit OCR call.
    """

    def __init__(
        self,
        *,
        engine_factory: Callable[[], Any] | None = None,
        max_spans: int = 2_000,
        max_span_chars: int = 2_000,
    ) -> None:
        if max_spans < 1 or max_span_chars < 1:
            raise ValueError("OCR 输出限制必须大于 0")
        self._engine_factory = engine_factory
        self._engine: Any | None = None
        self._max_spans = max_spans
        self._max_span_chars = max_span_chars
        self._jobs: dict[asyncio.Task[OCRResult], asyncio.Event] = {}
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def extract(self, frame: ImageFrame) -> OCRResult:
        if self._closed:
            raise PerceptionError("RapidOCR provider 已关闭")
        owned_image = bytearray(frame.data)
        worker = asyncio.create_task(
            asyncio.to_thread(self._extract_sync, owned_image),
            name="rapidocr-inference",
        )
        cleanup_finished = asyncio.Event()
        self._jobs[worker] = cleanup_finished
        try:
            return await await_owned_job(worker, discard=OCRResult.wipe)
        except PerceptionError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PerceptionError("本地 OCR 处理失败") from exc
        finally:
            wipe_buffer(owned_image)
            self._jobs.pop(worker, None)
            cleanup_finished.set()

    def _extract_sync(self, image_buffer: bytearray) -> OCRResult:
        engine = self._get_engine()
        # RapidOCR's public input contract expects immutable bytes.  This copy
        # exists only inside the owned worker; cancellation waits for the worker
        # before the mutable source is zeroed and observe() can return.
        output: Any = engine(bytes(image_buffer))
        spans = self._parse_output(output)
        return OCRResult(spans=spans[: self._max_spans])

    def _get_engine(self) -> Any:
        if self._engine is None:
            if self._engine_factory is not None:
                self._engine = self._engine_factory()
            else:
                try:
                    module: Any = importlib.import_module("rapidocr")
                    self._engine = module.RapidOCR()
                except (ImportError, AttributeError) as exc:
                    raise PerceptionError(
                        "RapidOCR 未安装；请安装可选依赖 rapidocr 与 onnxruntime"
                    ) from exc
        return self._engine

    def _parse_output(self, output: Any) -> list[OCRSpan]:
        if output is None:
            return []
        texts = getattr(output, "txts", None)
        scores = getattr(output, "scores", None)
        boxes = getattr(output, "boxes", None)
        if isinstance(texts, Sequence) and not isinstance(texts, (str, bytes)):
            return self._from_columns(texts, scores, boxes)

        legacy = output[0] if isinstance(output, tuple) and output else output
        if not isinstance(legacy, Sequence) or isinstance(legacy, (str, bytes)):
            raise PerceptionError("RapidOCR 返回格式无效")
        spans: list[OCRSpan] = []
        for row in legacy:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 3:
                raise PerceptionError("RapidOCR legacy row 格式无效")
            text = str(row[1])[: self._max_span_chars]
            confidence = self._confidence(row[2])
            spans.append(OCRSpan(text=text, confidence=confidence, box=self._box(row[0])))
        return spans

    def _from_columns(self, texts: Sequence[Any], scores: Any, boxes: Any) -> list[OCRSpan]:
        score_values = scores if isinstance(scores, Sequence) else ()
        box_values = boxes if isinstance(boxes, Sequence) else ()
        spans: list[OCRSpan] = []
        for index, text_value in enumerate(texts[: self._max_spans]):
            score = score_values[index] if index < len(score_values) else 0.0
            box = box_values[index] if index < len(box_values) else None
            spans.append(
                OCRSpan(
                    text=str(text_value)[: self._max_span_chars],
                    confidence=self._confidence(score),
                    box=self._box(box),
                )
            )
        return spans

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _box(value: Any) -> Rect | None:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return None
        try:
            points = [(float(point[0]), float(point[1])) for point in value if len(point) >= 2]
        except (TypeError, ValueError):
            return None
        if not points:
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        width = max(1, round(max(xs) - min(xs)))
        height = max(1, round(max(ys) - min(ys)))
        return Rect(x=round(min(xs)), y=round(min(ys)), width=width, height=height)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed and not self._jobs:
                return
            self._closed = True
            try:
                jobs = tuple(self._jobs.items())
                await drain_owned_jobs(
                    tuple(job for job, _cleanup in jobs),
                    cleanup_events=tuple(cleanup for _job, cleanup in jobs),
                )
            finally:
                self._engine = None
