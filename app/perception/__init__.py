"""Privacy-first, platform-independent screen perception."""

from app.perception.factory import build_perception_pipeline
from app.perception.models import ObservationResult, ObservationStatus
from app.perception.pipeline import PerceptionPipeline, PerceptionPipelineConfig
from app.perception.prompt_context import ApprovedVisualSummary, VisualSummaryLabel

__all__ = [
    "ObservationResult",
    "ObservationStatus",
    "ApprovedVisualSummary",
    "PerceptionPipeline",
    "PerceptionPipelineConfig",
    "VisualSummaryLabel",
    "build_perception_pipeline",
]
