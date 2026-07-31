"""Provider-neutral TTS contracts and concrete adapters."""

from app.clients.tts.base import TTSProvider
from app.clients.tts.gateway import GPTSoVITSGatewayProvider
from app.clients.tts.gpt_sovits import GPTSoVITSPreset, GPTSoVITSProbe, GPTSoVITSProvider
from app.clients.tts.mock_tts import MockTTSProvider

__all__ = [
    "GPTSoVITSProbe",
    "GPTSoVITSGatewayProvider",
    "GPTSoVITSProvider",
    "GPTSoVITSPreset",
    "MockTTSProvider",
    "TTSProvider",
]
