"""Runtime dependency composition for the modular monolith."""

from __future__ import annotations

from datetime import timedelta

from app.clients.llm import MockLLMProvider, OpenAICompatibleLLMProvider
from app.clients.llm.base import LLMProvider
from app.clients.tts import GPTSoVITSPreset, GPTSoVITSProvider, MockTTSProvider, TTSProvider
from app.clients.vts import (
    DPAPITokenStore,
    ExpressionMapper,
    TokenStore,
    VTSBridge,
    VTSClient,
    VTSTurnEventSink,
)
from app.config import ConfigurationError, Settings
from app.emotion import EmotionEngine, EmotionSegmentDecorator, ExpressionCooldown, SystemClock
from app.pipelines import DialoguePipeline
from app.pipelines.audio_player import AudioPlayer, SilentAudioPlayer, SystemAudioPlayer
from app.prompts import (
    EmotionPromptContextBuilder,
    PromptBudget,
    PromptBuilder,
    PromptContextSource,
)
from app.prompts.tokens import ProviderTokenEstimator
from app.secret_store import LLM_API_KEY_ID, EncryptedSecretFile, llm_api_key_file, vts_token_file
from app.temp_assets import TempAssetRegistry


def build_llm_provider(
    settings: Settings,
    *,
    secret_file: EncryptedSecretFile | None = None,
) -> LLMProvider | None:
    """Build the configured provider without a silent fallback to a mock."""

    settings.validate_runtime_limits()
    provider_name = settings.llm.provider.strip().lower()
    if provider_name == "none":
        return None
    if provider_name == "mock":
        return MockLLMProvider(token_delay_seconds=settings.pipeline.mock_token_delay_ms / 1000)
    if not settings.llm.model.strip():
        raise RuntimeError("真实 LLM provider 已启用，但 llm.model 为空。")
    api_key = _resolve_llm_api_key(settings, secret_file=secret_file)
    return OpenAICompatibleLLMProvider(
        base_url=settings.llm.base_url,
        endpoint=settings.llm.endpoint,
        model=settings.llm.model,
        api_key=api_key,
        timeout_seconds=settings.llm.timeout_seconds,
        default_temperature=settings.llm.temperature,
        default_max_tokens=min(
            settings.llm.max_tokens,
            settings.limits.provider_output_tokens,
        ),
        max_stream_event_bytes=settings.limits.llm_output_bytes,
        stream_completion_mode=settings.llm.stream_completion_mode,
        proxy_url=settings.llm.transport.proxy_url,
        ca_bundle_path=settings.llm_ca_bundle_path(),
    )


def build_dialogue_pipeline(
    settings: Settings,
    *,
    prompt_context_source: PromptContextSource | None = None,
    llm_provider: LLMProvider | None = None,
    temp_registry: TempAssetRegistry | None = None,
) -> DialoguePipeline | None:
    settings.validate_runtime_limits()
    llm = llm_provider or build_llm_provider(settings)
    if llm is None:
        return None

    tts = _build_tts(settings, temp_registry=temp_registry)
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
        PromptBuilder(
            budget=PromptBudget(
                total_tokens=settings.limits.prompt_total_tokens,
                system_tokens=settings.limits.prompt_system_tokens,
                current_user_tokens=settings.limits.prompt_current_tokens,
                history_tokens=settings.limits.prompt_history_tokens,
                memory_tokens=settings.limits.prompt_memory_tokens,
                screen_tokens=settings.limits.prompt_screen_tokens,
                max_block_tokens=settings.limits.prompt_block_tokens,
                provider_output_tokens=min(
                    settings.llm.max_tokens,
                    settings.limits.provider_output_tokens,
                ),
            ),
            estimator=ProviderTokenEstimator(
                provider=settings.llm.provider,
                model=settings.llm.model,
            ),
        ),
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
        limits=settings.limits,
        tts_connect_timeout_ms=round(settings.tts.connect_timeout_seconds * 1000),
        tts_first_byte_timeout_ms=round(settings.tts.first_byte_timeout_seconds * 1000),
        tts_total_timeout_ms=round(settings.tts.timeout_seconds * 1000),
        tts_cancellation_timeout_ms=round(settings.tts.cancellation_timeout_seconds * 1000),
    )


def _build_tts(
    settings: Settings,
    *,
    temp_registry: TempAssetRegistry | None,
) -> TTSProvider:
    provider_name = settings.tts.provider.strip().lower()
    if provider_name == "mock":
        cache_path = settings.mock_audio_directory()
        return MockTTSProvider(
            cache_path,
            duration_ms=settings.pipeline.mock_audio_duration_ms,
            volume=settings.pipeline.mock_audio_volume,
            temp_registry=temp_registry,
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
        settings.tts_output_directory(),
        presets,
        default_preset=settings.tts.default_preset,
        max_audio_bytes=settings.tts.max_audio_bytes,
        cache_enabled=settings.tts.cache_enabled,
        cache_dir=settings.tts_cache_directory(),
        cache_max_bytes=settings.tts.cache_max_bytes,
        cache_ttl_seconds=settings.tts.cache_ttl_seconds,
        proxy_url=settings.tts.transport.proxy_url,
        ca_bundle_path=settings.tts_ca_bundle_path(),
        temp_registry=temp_registry,
    )


def build_vts_event_sink(
    settings: Settings,
    *,
    token_store: TokenStore | None = None,
) -> VTSTurnEventSink | None:
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
            proxy_url=settings.vts.transport.proxy_url,
            ca_bundle_path=settings.vts_ca_bundle_path(),
        ),
        token_store
        or DPAPITokenStore(
            vts_token_file(
                settings.paths,
                settings.vts_token_path(),
            )
        ),
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


def _resolve_llm_api_key(
    settings: Settings,
    *,
    secret_file: EncryptedSecretFile | None,
) -> str:
    if settings.app.environment != "prod" and settings.llm.api_key_env:
        try:
            return settings.require_llm_api_key().get_secret_value()
        except ConfigurationError:
            pass
    encrypted = secret_file or llm_api_key_file(settings.paths)
    value = encrypted.read_text()
    if value is None:
        raise ConfigurationError(
            f"缺少必需的 DPAPI secret_id={LLM_API_KEY_ID}；"
            "请使用显式 secret import 命令重新输入，生产不会读取环境变量。"
        )
    return value
