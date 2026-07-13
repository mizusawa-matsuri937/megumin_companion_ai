"""Internal, privacy-scoped models for screen perception.

Raw titles, OCR spans, and image bytes intentionally live only in this module
boundary.  Public consumers receive ``app.schemas.ai.PerceptionContext``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.schemas.ai import PerceptionContext


class PerceptionError(RuntimeError):
    """An implementation failure whose message must not include captured content."""


class ObservationStatus(StrEnum):
    disabled = "disabled"
    blocked_before_capture = "blocked_before_capture"
    blocked_after_ocr = "blocked_after_ocr"
    unchanged = "unchanged"
    analyzed_local = "analyzed_local"
    analyzed_cloud = "analyzed_cloud"
    cloud_rate_limited = "cloud_rate_limited"
    guard_error = "guard_error"
    capture_error = "capture_error"
    ocr_error = "ocr_error"
    processing_error = "processing_error"
    cloud_error = "cloud_error"


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("矩形宽高必须大于 0")


@dataclass(frozen=True, slots=True)
class WindowInfo:
    window_id: str
    title: str
    process_name: str
    rect: Rect

    def __post_init__(self) -> None:
        if not self.window_id:
            raise ValueError("window_id 不能为空")


@dataclass(slots=True)
class ImageFrame:
    data: bytearray
    width: int
    height: int
    content_type: str = "image/png"
    perceptual_hash: int | None = None

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("图片数据不能为空")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("图片尺寸必须大于 0")
        if self.content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("不支持的图片格式")

    def wipe(self) -> None:
        self.data[:] = b"\x00" * len(self.data)
        self.data.clear()
        self.perceptual_hash = None


@dataclass(frozen=True, slots=True)
class FrameChangeAssessment:
    """Opaque comparison result committed only after privacy processing succeeds."""

    window_key: bytes
    digest: bytes
    perceptual_hash: int | None
    changed: bool
    previous_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class OCRSpan:
    text: str
    confidence: float
    box: Rect | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("OCR confidence 必须在 0 到 1 之间")


@dataclass(slots=True)
class OCRResult:
    spans: list[OCRSpan] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(span.text for span in self.spans if span.text)

    def wipe(self) -> None:
        self.spans.clear()


class GuardOutcome(StrEnum):
    allow = "allow"
    block = "block"


@dataclass(frozen=True, slots=True)
class WindowGuardDecision:
    outcome: GuardOutcome
    category: str
    reason_code: str
    process_fingerprint: str


@dataclass(frozen=True, slots=True)
class ContentGuardDecision:
    outcome: GuardOutcome
    reason_codes: tuple[str, ...] = ()
    redaction_regions: tuple[Rect, ...] = ()


@dataclass(frozen=True, slots=True)
class SceneAnalysis:
    category: str
    summary: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("场景摘要不能为空")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("场景置信度必须在 0 到 1 之间")


@dataclass(frozen=True, slots=True)
class CloudAnalysis:
    summary: str
    confidence: float
    category: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationResult:
    status: ObservationStatus
    context: PerceptionContext | None = None
    reason_code: str | None = None
