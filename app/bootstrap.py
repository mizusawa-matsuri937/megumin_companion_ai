"""Runtime dependency composition for the modular monolith."""

from __future__ import annotations

from pathlib import Path

from app.clients.llm import MockLLMProvider, OpenAICompatibleLLMProvider
from app.clients.llm.base import LLMProvider
from app.clients.tts import GPTSoVITSPreset, GPTSoVITSProvider, MockTTSProvider, TTSProvider
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

    tts = _build_tts(settings)
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


def _build_tts(settings: Settings) -> TTSProvider:
    provider_name = settings.tts.provider.strip().lower()
    if provider_name == "mock":
        cache_path = _project_path(settings.pipeline.audio_cache_path)
        return MockTTSProvider(
            cache_path,
            duration_ms=settings.pipeline.mock_audio_duration_ms,
            volume=settings.pipeline.mock_audio_volume,
        )
    if provider_name not in {"gpt-sovits", "gpt_sovits"}:
        raise RuntimeError(f"不支持的 TTS provider：{settings.tts.provider}")
    if settings.tts.default_preset not in settings.tts.presets:
        raise RuntimeError("GPT-SoVITS 已启用，但 default_preset 未配置。")
    presets = {
        name: GPTSoVITSPreset(**preset.model_dump())
        for name, preset in settings.tts.presets.items()
    }
    return GPTSoVITSProvider(
        settings.tts.base_url,
        _project_path(settings.tts.output_directory),
        presets,
        default_preset=settings.tts.default_preset,
        timeout_seconds=settings.tts.timeout_seconds,
        max_audio_bytes=settings.tts.max_audio_bytes,
        cache_enabled=settings.tts.cache_enabled,
        cache_dir=_project_path(settings.tts.cache_directory),
        cache_max_bytes=settings.tts.cache_max_bytes,
        cache_ttl_seconds=settings.tts.cache_ttl_seconds,
    )


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path
