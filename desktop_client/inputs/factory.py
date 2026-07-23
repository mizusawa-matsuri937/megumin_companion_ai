"""Build opt-in local PTT without opening a microphone during startup."""

from __future__ import annotations

from app.config import Settings
from app.media.voice import create_media_worker_voice_input
from app.temp_assets import TempAssetRegistry

from desktop_client.inputs.voice_input import PushToTalkRecorder


def build_voice_input(
    settings: Settings,
    *,
    temp_registry: TempAssetRegistry | None = None,
) -> PushToTalkRecorder | None:
    """Return an explicitly enabled PTT controller; device open is deferred to ``start``."""

    capture = create_media_worker_voice_input(settings, temp_registry=temp_registry)
    return PushToTalkRecorder(capture) if capture is not None else None
