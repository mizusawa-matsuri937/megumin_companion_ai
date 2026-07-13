"""Optional Pillow-based resizing and irreversible box masking."""

from __future__ import annotations

import asyncio
import importlib
import io
from dataclasses import dataclass
from typing import Any

from app.perception.models import ImageFrame, PerceptionError, Rect
from app.perception.thread_jobs import (
    await_owned_job,
    drain_owned_jobs,
    wipe_buffer,
)


@dataclass(frozen=True, slots=True)
class PillowSanitizerConfig:
    max_dimension: int = 1_280
    max_pixels: int = 12_000_000
    max_input_bytes: int = 20 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_dimension < 1 or self.max_pixels < 1 or self.max_input_bytes < 1:
            raise ValueError("图片处理限制必须大于 0")


class PillowImageSanitizer:
    def __init__(self, config: PillowSanitizerConfig | None = None) -> None:
        self._config = config or PillowSanitizerConfig()
        self._jobs: dict[asyncio.Task[ImageFrame], asyncio.Event] = {}
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def sanitize(self, frame: ImageFrame, regions: tuple[Rect, ...]) -> ImageFrame:
        if self._closed:
            raise PerceptionError("图片 sanitizer 已关闭")
        owned_image = bytearray(frame.data)
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._sanitize_sync,
                owned_image,
                frame.width,
                frame.height,
                regions,
            ),
            name="pillow-image-sanitize",
        )
        cleanup_finished = asyncio.Event()
        self._jobs[worker] = cleanup_finished
        try:
            return await await_owned_job(worker, discard=ImageFrame.wipe)
        except PerceptionError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PerceptionError("图片隐私处理失败") from exc
        finally:
            wipe_buffer(owned_image)
            self._jobs.pop(worker, None)
            cleanup_finished.set()

    def _sanitize_sync(
        self,
        image_buffer: bytearray,
        expected_width: int,
        expected_height: int,
        regions: tuple[Rect, ...],
    ) -> ImageFrame:
        if len(image_buffer) > self._config.max_input_bytes:
            raise PerceptionError("待处理图片超过大小限制")
        try:
            image_module: Any = importlib.import_module("PIL.Image")
            draw_module: Any = importlib.import_module("PIL.ImageDraw")
        except ImportError as exc:
            raise PerceptionError("Pillow 未安装；无法进行图片遮挡处理") from exc

        with image_module.open(io.BytesIO(image_buffer)) as opened:
            original_width, original_height = int(opened.width), int(opened.height)
            if (
                original_width <= 0
                or original_height <= 0
                or original_width * original_height > self._config.max_pixels
            ):
                raise PerceptionError("待处理图片像素数超过限制")
            if (original_width, original_height) != (expected_width, expected_height):
                raise PerceptionError("图片尺寸元数据与实际内容不一致")
            opened.load()
            image = opened.convert("RGB")
            image.thumbnail((self._config.max_dimension, self._config.max_dimension))
            scale_x = image.width / original_width
            scale_y = image.height / original_height
            drawer = draw_module.Draw(image)
            for region in regions:
                if (
                    region.x < 0
                    or region.y < 0
                    or region.x + region.width > original_width
                    or region.y + region.height > original_height
                ):
                    raise PerceptionError("图片遮挡区域超出边界")
                drawer.rectangle(
                    (
                        round(region.x * scale_x),
                        round(region.y * scale_y),
                        round((region.x + region.width) * scale_x),
                        round((region.y + region.height) * scale_y),
                    ),
                    fill=(0, 0, 0),
                )
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            return ImageFrame(
                data=bytearray(output.getvalue()),
                width=int(image.width),
                height=int(image.height),
                content_type="image/png",
            )

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed and not self._jobs:
                return
            self._closed = True
            jobs = tuple(self._jobs.items())
            await drain_owned_jobs(
                tuple(job for job, _cleanup in jobs),
                cleanup_events=tuple(cleanup for _job, cleanup in jobs),
            )
