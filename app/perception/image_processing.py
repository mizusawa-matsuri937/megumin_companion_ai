"""Optional Pillow-based resizing and irreversible box masking."""

from __future__ import annotations

import asyncio
import importlib
import io
from dataclasses import dataclass
from typing import Any

from app.perception.models import ImageFrame, PerceptionError, Rect


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

    async def sanitize(self, frame: ImageFrame, regions: tuple[Rect, ...]) -> ImageFrame:
        try:
            return await asyncio.to_thread(self._sanitize_sync, frame, regions)
        except PerceptionError:
            raise
        except Exception as exc:
            raise PerceptionError("图片隐私处理失败") from exc

    def _sanitize_sync(self, frame: ImageFrame, regions: tuple[Rect, ...]) -> ImageFrame:
        if len(frame.data) > self._config.max_input_bytes:
            raise PerceptionError("待处理图片超过大小限制")
        try:
            image_module: Any = importlib.import_module("PIL.Image")
            draw_module: Any = importlib.import_module("PIL.ImageDraw")
        except ImportError as exc:
            raise PerceptionError("Pillow 未安装；无法进行图片遮挡处理") from exc

        with image_module.open(io.BytesIO(frame.data)) as opened:
            original_width, original_height = int(opened.width), int(opened.height)
            if (
                original_width <= 0
                or original_height <= 0
                or original_width * original_height > self._config.max_pixels
            ):
                raise PerceptionError("待处理图片像素数超过限制")
            opened.load()
            image = opened.convert("RGB")
            image.thumbnail((self._config.max_dimension, self._config.max_dimension))
            scale_x = image.width / original_width
            scale_y = image.height / original_height
            drawer = draw_module.Draw(image)
            for region in regions:
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
