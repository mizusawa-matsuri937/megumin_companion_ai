"""Central W07 hard limits shared by transport, prompts, and dialogue runtime."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LimitsConfig(BaseModel):
    """Versioned hard caps; provider settings may be lower but never higher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1, le=1)

    dev_http_body_bytes: int = Field(default=64 * 1024, ge=256, le=64 * 1024)
    dev_websocket_frame_bytes: int = Field(default=64 * 1024, ge=256, le=64 * 1024)
    metadata_bytes: int = Field(default=8 * 1024, ge=64, le=8 * 1024)
    metadata_keys: int = Field(default=16, ge=1, le=16)
    metadata_depth: int = Field(default=3, ge=1, le=3)
    metadata_nodes: int = Field(default=128, ge=1, le=128)

    provider_output_tokens: int = Field(default=4_096, ge=1, le=100_000)
    prompt_total_tokens: int = Field(default=16_384, ge=256, le=100_000)
    prompt_system_tokens: int = Field(default=4_096, ge=128, le=100_000)
    prompt_current_tokens: int = Field(default=8_192, ge=32, le=100_000)
    prompt_history_tokens: int = Field(default=4_096, ge=0, le=100_000)
    prompt_memory_tokens: int = Field(default=2_048, ge=0, le=100_000)
    prompt_screen_tokens: int = Field(default=1_024, ge=0, le=100_000)
    prompt_block_tokens: int = Field(default=512, ge=16, le=20_000)

    llm_output_bytes: int = Field(default=64 * 1024, ge=1, le=64 * 1024)
    llm_output_segments: int = Field(default=128, ge=1, le=128)
    tts_queue_capacity: int = Field(default=8, ge=1, le=8)
    ready_audio_queue_capacity: int = Field(default=4, ge=1, le=4)
    audio_single_result_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=44,
        le=64 * 1024 * 1024,
    )
    audio_inflight_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=44,
        le=64 * 1024 * 1024,
    )
    audio_total_duration_ms: int = Field(default=120_000, ge=1, le=120_000)

    @model_validator(mode="after")
    def validate_internal_bounds(self) -> LimitsConfig:
        if self.audio_single_result_bytes > self.audio_inflight_bytes:
            raise ValueError("audio single-result hard limit exceeds in-flight hard limit")
        if self.prompt_system_tokens + 1 > self.prompt_total_tokens:
            raise ValueError("prompt system hard limit leaves no current-user token")
        return self
