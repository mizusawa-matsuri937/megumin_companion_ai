"""Narrow shared contracts for the fixed local GPT-SoVITS gateway."""

from app.tts_gateway.contracts import (
    GATEWAY_PROTOCOL_VERSION,
    VOICE_SLOTS,
    GatewayHealth,
    GatewayTTSRequest,
    VoiceSlot,
)

__all__ = [
    "GATEWAY_PROTOCOL_VERSION",
    "VOICE_SLOTS",
    "GatewayHealth",
    "GatewayTTSRequest",
    "VoiceSlot",
]
