"""Runtime dependency composition for the modular monolith."""

from __future__ import annotations

from datetime import timedelta

from app.clients.llm import MockLLMProvider, OpenAICompatibleLLMProvider
from app.clients.llm.base import LLMProvider
from app.clients.tts import GPTSoVITSPreset, GPTSoVITSProvider, MockTTSProvider, TTSProvider
from app.clients.vts import (
    ExpressionMapper,
    FileTokenStore,
    VTSBridge,
    VTSClient,
    VTSTurnEventSink,
)
from app.config import Settings
from app.emotion import EmotionEngine, EmotionSegmentDecorator, ExpressionCooldown, SystemClock
from app.pipelines import DialoguePipeline
from app.pipelines.audio_player import AudioPlayer, SilentAudioPlayer, SystemAudioPlayer
from app.prompts import EmotionPromptContextBuilder, PromptBuilder, PromptContextSource


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    """Build the configured provider without a silent fallback to a mock."""

    provider_name = settings.llm.provider.strip().lower()
    if provider_name == "none":
        return None
    if provider_name == "mock":
        return MockLLMProvider(token_delay_seconds=settings.pipeline.mock_token_delay_ms / 1000)
    if not settings.llm.model.strip():
        raise RuntimeError("真实 LLM provider 已启用，但 llm.model 为空。")
    api_key = settings.require_llm_api_key().get_secret_value()
    return OpenAICompatibleLLMProvider(
        base_url=settings.llm.base_url,
        endpoint=settings.llm.endpoint,
        model=settings.llm.model,
        api_key=api_key,
        timeout_seconds=settings.llm.timeout_seconds,
        default_temperature=settings.llm.temperature,
        default_max_tokens=settings.llm.max_tokens,
    )


def build_dialogue_pipeline(
    settings: Settings,
    *,
    prompt_context_source: PromptContextSource | None = None,
    llm_provider: LLMProvider | None = None,
) -> DialoguePipeline | None:
    llm = llm_provider or build_llm_provider(settings)
    if llm is None:
        return None

    tts = _build_tts(settings)
    player: AudioPlayer
    if settings.pipeline.playback_mode == "system":
        player = SystemAudioPlayer()
    else:
        player = SilentAudioPlayer()
    clock = SystemClock()
    emotion_engine = EmotionEngine(
        clock=clock,
        max_delta_per_event=settings.emotion.max_delta_per_event,
        max_delta_per_minute=settings.emotion.max_delta_per_minute,
        label_min_duration=timedelta(seconds=settings.emotion.label_min_duration_seconds),
        explosion_cooldown=timedelta(seconds=settings.emotion.explosion_cooldown_seconds),
    )
    context_builder = EmotionPromptContextBuilder(
        PromptBuilder(),
        emotion_engine,
        clock,
        source=prompt_context_source,
        update_emotion=settings.emotion.enabled,
    )
    proactive_context_builder = context_builder
    segment_decorator = None
    if settings.emotion.enabled:
        segment_decorator = EmotionSegmentDecorator(
            emotion_engine,
            ExpressionCooldown(
                clock=clock,
                cooldown=timedelta(seconds=settings.emotion.expression_cooldown_seconds),
            ),
        )
    return DialoguePipeline(
        llm,
        tts,
        player,
        context_builder=context_builder,
        proactive_context_builder=proactive_context_builder,
        segment_decorator=segment_decorator,
        tts_worker_count=settings.pipeline.tts_worker_count,
        segment_min_chars=settings.pipeline.segment_min_chars,
        segment_max_chars=settings.pipeline.segment_max_chars,
        segment_max_words=settings.pipeline.segment_max_words,
    )


def _build_tts(settings: Settings) -> TTSProvider:
    provider_name = settings.tts.provider.strip().lower()
    if provider_name == "mock":
        cache_path = settings.resolve_runtime_path(settings.pipeline.audio_cache_path)
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
        settings.resolve_runtime_path(settings.tts.output_directory),
        presets,
        default_preset=settings.tts.default_preset,
        timeout_seconds=settings.tts.timeout_seconds,
        max_audio_bytes=settings.tts.max_audio_bytes,
        cache_enabled=settings.tts.cache_enabled,
        cache_dir=settings.resolve_runtime_path(settings.tts.cache_directory),
        cache_max_bytes=settings.tts.cache_max_bytes,
        cache_ttl_seconds=settings.tts.cache_ttl_seconds,
    )


def build_vts_event_sink(settings: Settings) -> VTSTurnEventSink | None:
    if not settings.vts.enabled:
        return None
    mapper = (
        ExpressionMapper(settings.vts.expression_hotkeys)
        if settings.vts.expression_hotkeys
        else ExpressionMapper()
    )
    bridge = VTSBridge(
        lambda: VTSClient(
            settings.vts.uri,
            request_timeout_seconds=settings.vts.request_timeout_seconds,
        ),
        FileTokenStore(settings.resolve_runtime_path(settings.vts.token_path)),
        plugin_name=settings.vts.plugin_name,
        plugin_developer=settings.vts.plugin_developer,
        expression_mapper=mapper,
        queue_capacity=settings.vts.queue_capacity,
        reconnect_initial_seconds=settings.vts.reconnect_initial_seconds,
        reconnect_max_seconds=settings.vts.reconnect_max_seconds,
    )
    sink = VTSTurnEventSink(bridge)
    sink.start()
    return sink
