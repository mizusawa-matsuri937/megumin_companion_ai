"""TTS provider contracts and the Phase 1 deterministic mock."""

from app.clients.tts.base import TTSProvider
from app.clients.tts.mock_tts import MockTTSProvider

__all__ = ["MockTTSProvider", "TTSProvider"]
