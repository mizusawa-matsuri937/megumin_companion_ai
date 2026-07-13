"""Runtime dependency composition for the modular monolith."""

from __future__ import annotations

from app.clients.llm import MockLLMProvider, OpenAICompatibleLLMProvider
from app.clients.llm.base import LLMProvider
from app.clients.tts import MockTTSProvider
from app.config import Settings
from app.config.settings import PROJECT_ROOT
from app.pipelines import DialoguePipeline
from app.pipelines.audio_player import AudioPlayer, SilentAudioPlayer, SystemAudioPlayer


def build_dialogue_pipeline(settings: Settings) -> DialoguePipeline | None:
    provider_name = settings.llm.provider.strip().lower()
    if provider_name == "none":
        return None

    llm: LLMProvider
    if provider_name == "mock":
        llm = MockLLMProvider(token_delay_seconds=settings.pipeline.mock_token_delay_ms / 1000)
    else:
        if not settings.llm.model.strip():
            raise RuntimeError("真实 LLM provider 已启用，但 llm.model 为空。")
        api_key = settings.require_llm_api_key().get_secret_value()
        llm = OpenAICompatibleLLMProvider(
            base_url=settings.llm.base_url,
            endpoint=settings.llm.endpoint,
            model=settings.llm.model,
            api_key=api_key,
            timeout_seconds=settings.llm.timeout_seconds,
            default_temperature=settings.llm.temperature,
            default_max_tokens=settings.llm.max_tokens,
        )

    cache_path = settings.pipeline.audio_cache_path
    if not cache_path.is_absolute():
        cache_path = PROJECT_ROOT / cache_path
    tts = MockTTSProvider(
        cache_path,
        duration_ms=settings.pipeline.mock_audio_duration_ms,
        volume=settings.pipeline.mock_audio_volume,
    )
    player: AudioPlayer
    if settings.pipeline.playback_mode == "system":
        player = SystemAudioPlayer()
    else:
        player = SilentAudioPlayer()
    return DialoguePipeline(
        llm,
        tts,
        player,
        tts_worker_count=settings.pipeline.tts_worker_count,
        segment_min_chars=settings.pipeline.segment_min_chars,
        segment_max_chars=settings.pipeline.segment_max_chars,
        segment_max_words=settings.pipeline.segment_max_words,
    )
