"""Small path-free HTTP contracts exposed by the private TTS gateway."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GATEWAY_PROTOCOL_VERSION = 1
VOICE_SLOTS = frozenset(
    {
        "neutral",
        "gentle",
        "tsundere",
        "focused",
        "excited_explosion",
    }
)
VoiceSlot = Literal[
    "neutral",
    "gentle",
    "tsundere",
    "focused",
    "excited_explosion",
]


class GatewayTTSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=20_000)
    voice_slot: VoiceSlot
    speed_factor: float = Field(ge=0.5, le=2.0)


class GatewayHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = 1
    status: Literal["ready", "quarantine"]
    current_slot: VoiceSlot | None
