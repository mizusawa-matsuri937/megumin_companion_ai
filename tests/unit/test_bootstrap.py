"""Dependency composition tests for disabled, mock, and real provider modes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.avatar import AvatarRuntime
from app.bootstrap import (
    build_avatar_runtime,
    build_dialogue_pipeline,
    build_llm_provider,
    build_vts_event_sink,
)
from app.clients.llm import MockLLMProvider, OpenAICompatibleLLMProvider
from app.clients.tts import GPTSoVITSGatewayProvider, GPTSoVITSProvider
from app.clients.vts import VTSBridgeSnapshot, VTSBridgeState
from app.config import Settings
from app.config.settings import (
    AppConfig,
    AvatarConfig,
    EmotionConfig,
    GPTSoVITSPresetConfig,
    LLMConfig,
    PipelineConfig,
    ProviderTransportConfig,
    TTSConfig,
    VTSConfig,
)
from app.media import MediaWorkerAudioPlayer
from app.paths import AppPaths
from app.prompts import EmotionPromptContextBuilder, HistoryMessage, PromptContextSnapshot
from app.schemas import ChatRole, ExternalContextBlock, UserMessage
from app.schemas.ai import ContextOrigin, ContextTrust
from app.secret_store import llm_api_key_file, tts_gateway_token_file
from app.windows_security import PortableDirectorySecurity


class _ReversingProtector:
    @property
    def algorithm(self) -> str:
        return "test-reverse"

    @property
    def scope(self) -> str:
        return "current_user"

    def protect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        del purpose, key_id
        return value[::-1]

    def unprotect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        del purpose, key_id
        return value[::-1]


def test_disabled_provider_builds_no_pipeline() -> None:
    assert build_llm_provider(Settings(llm=LLMConfig(provider=" NONE "))) is None
    assert build_dialogue_pipeline(Settings(llm=LLMConfig(provider=" NONE "))) is None


def test_mock_provider_factory_is_explicit() -> None:
    provider = build_llm_provider(Settings(llm=LLMConfig(provider=" mock ")))
    assert isinstance(provider, MockLLMProvider)
    asyncio.run(provider.close())


def test_real_provider_requires_model() -> None:
    with pytest.raises(RuntimeError, match="model 为空"):
        build_dialogue_pipeline(Settings(llm=LLMConfig(provider="openai", model="   ")))


def test_real_provider_managed_cache_and_media_worker_player(tmp_path: Path) -> None:
    settings = Settings(
        llm=LLMConfig(
            provider="compatible",
            model="test-model",
            api_key_env="TEST_KEY",
            temperature=0.65,
            max_tokens=777,
        ),
        pipeline=PipelineConfig(audio_cache_path=Path("mock"), playback_mode="system"),
    )
    settings._environment = {"TEST_KEY": "fake-test-key"}
    settings._paths = AppPaths(root=tmp_path / "AppData")

    def listener(_sample: object) -> None:
        return None

    pipeline = build_dialogue_pipeline(
        settings,
        mouth_envelope_listener=listener,
    )

    assert pipeline is not None
    assert isinstance(pipeline._audio_player, MediaWorkerAudioPlayer)
    assert pipeline._audio_player._mouth_envelope_listener is listener
    assert isinstance(pipeline._llm, OpenAICompatibleLLMProvider)
    assert pipeline._llm._default_temperature == 0.65
    assert pipeline._llm._default_max_tokens == 777
    asyncio.run(pipeline.close())


def test_avatar_runtime_composition_requires_both_vts_and_avatar() -> None:
    assert build_avatar_runtime(Settings()) is None
    assert (
        build_avatar_runtime(
            Settings(
                vts=VTSConfig(enabled=True),
                avatar=AvatarConfig(enabled=False),
            )
        )
        is None
    )

    runtime = build_avatar_runtime(Settings(vts=VTSConfig(enabled=True)))

    assert isinstance(runtime, AvatarRuntime)
    asyncio.run(runtime.close())


def test_production_real_provider_ignores_environment_and_uses_encrypted_secret(
    tmp_path: Path,
) -> None:
    paths = AppPaths(root=tmp_path / "AppData")
    encrypted = llm_api_key_file(
        paths,
        protector=_ReversingProtector(),
        directory_security=PortableDirectorySecurity(),
    )
    encrypted.write_text("encrypted-production-key")
    settings = Settings(
        app=AppConfig(environment="prod"),
        llm=LLMConfig(provider="compatible", model="test-model", api_key_env="TEST_KEY"),
    )
    settings._environment = {"TEST_KEY": "ignored-development-key"}
    settings._paths = paths

    provider = build_llm_provider(settings, secret_file=encrypted)

    assert isinstance(provider, OpenAICompatibleLLMProvider)
    assert provider._client.headers["Authorization"] == "Bearer encrypted-production-key"
    asyncio.run(provider.close())


def test_gpt_sovits_wiring_is_explicit_and_cache_defaults_off(tmp_path: Path) -> None:
    settings = Settings(
        llm=LLMConfig(provider="mock"),
        tts=TTSConfig(
            provider="gpt-sovits",
            output_directory=Path("ephemeral"),
            cache_directory=Path("persistent"),
            presets={"default": GPTSoVITSPresetConfig(ref_audio_path="/local/reference.wav")},
        ),
    )
    settings._paths = AppPaths(root=tmp_path / "AppData")

    pipeline = build_dialogue_pipeline(settings)

    assert pipeline is not None
    assert isinstance(pipeline._tts, GPTSoVITSProvider)
    assert not pipeline._tts._cache_enabled
    assert pipeline._tts._max_owned_synthesis_tasks == settings.limits.tts_queue_capacity
    asyncio.run(pipeline.close())


def test_private_gateway_wiring_reads_distinct_encrypted_token(tmp_path: Path) -> None:
    paths = AppPaths(root=tmp_path / "AppData")
    encrypted = tts_gateway_token_file(
        paths,
        protector=_ReversingProtector(),
        directory_security=PortableDirectorySecurity(),
    )
    encrypted.write_text("A" * 43)
    settings = Settings(
        llm=LLMConfig(provider="mock"),
        tts=TTSConfig(
            provider="gpt-sovits-gateway",
            output_directory=Path("ephemeral"),
        ),
    )
    settings._paths = paths

    pipeline = build_dialogue_pipeline(
        settings,
        tts_gateway_secret_file=encrypted,
    )

    assert pipeline is not None
    assert isinstance(pipeline._tts, GPTSoVITSGatewayProvider)
    assert pipeline._tts._max_audio_bytes == settings.tts.max_audio_bytes
    asyncio.run(pipeline.close())


@pytest.mark.parametrize(
    "tts",
    [
        TTSConfig(provider="gpt-sovits-gateway", cache_enabled=True),
        TTSConfig(
            provider="gpt-sovits-gateway",
            transport=ProviderTransportConfig(proxy_url="http://127.0.0.1:8080"),
        ),
    ],
)
def test_private_gateway_rejects_cache_and_transport_overrides(
    tmp_path: Path,
    tts: TTSConfig,
) -> None:
    paths = AppPaths(root=tmp_path / "AppData")
    encrypted = tts_gateway_token_file(
        paths,
        protector=_ReversingProtector(),
        directory_security=PortableDirectorySecurity(),
    )
    encrypted.write_text("A" * 43)
    settings = Settings(llm=LLMConfig(provider="mock"), tts=tts)
    settings._paths = paths

    with pytest.raises(RuntimeError):
        build_dialogue_pipeline(
            settings,
            tts_gateway_secret_file=encrypted,
        )


def test_disabling_emotion_keeps_prompt_policy_and_external_context() -> None:
    class Source:
        def __init__(self) -> None:
            self.history_calls = 0
            self.context_calls = 0

        async def snapshot_for(self, _message: UserMessage) -> PromptContextSnapshot:
            self.history_calls += 1
            self.context_calls += 1
            return PromptContextSnapshot(
                history=(
                    HistoryMessage(message_id="prior", role=ChatRole.assistant, content="prior"),
                ),
                blocks=(
                    ExternalContextBlock(
                        source_id="memory-1",
                        origin=ContextOrigin.long_term_memory,
                        trust=ContextTrust.stored_fact,
                        content="remembered context",
                        persistable=False,
                    ),
                ),
            )

    source = Source()
    pipeline = build_dialogue_pipeline(
        Settings(llm=LLMConfig(provider="mock"), emotion=EmotionConfig(enabled=False)),
        prompt_context_source=source,
    )
    assert pipeline is not None
    assert isinstance(pipeline._context_builder, EmotionPromptContextBuilder)

    request = asyncio.run(
        pipeline._context_builder.build(UserMessage(user_id="user", text="hello"))
    )

    assert source.history_calls == 1
    assert source.context_calls == 1
    assert request.messages[0].role is ChatRole.system
    assert "private desktop companion" in str(request.messages[0].content)
    assert any("remembered context" in str(message.content) for message in request.messages)
    assert pipeline._segment_decorator is None
    asyncio.run(pipeline.close())


@pytest.mark.parametrize("provider", ["gpt-sovits", "unsupported"])
def test_tts_wiring_never_falls_back_to_mock(provider: str) -> None:
    settings = Settings(llm=LLMConfig(provider="mock"), tts=TTSConfig(provider=provider))

    with pytest.raises(RuntimeError):
        build_dialogue_pipeline(settings)


@pytest.mark.parametrize("hotkeys", [{}, {"happy": "custom-happy"}])
def test_vts_wiring_starts_bounded_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hotkeys: dict[str, str],
) -> None:
    class FakeVTSBridge:
        instances: list[FakeVTSBridge] = []

        def __init__(self, *_args: object, **options: object) -> None:
            self.args = _args
            self.options = options
            self.started = False
            self.closed = False
            self.instances.append(self)

        def start(self) -> None:
            self.started = True

        def begin_turn(self, turn_id: str) -> int | None:
            return 1 if turn_id else None

        def cancel_turn(self, turn_id: str, generation: int) -> bool:
            return bool(turn_id and generation == 1)

        def enqueue_expression(self, _expression: str, *, turn_id: str, generation: int) -> bool:
            return bool(turn_id and generation == 1)

        def snapshot(self) -> VTSBridgeSnapshot:
            return VTSBridgeSnapshot(
                state=VTSBridgeState.ready,
                queue_size=0,
                dropped_actions=0,
                processed_actions=0,
                reconnect_count=0,
                missing_expression_count=0,
            )

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("app.bootstrap.VTSBridge", FakeVTSBridge)
    settings = Settings(
        vts=VTSConfig(
            enabled=True,
            token_path=Path("token.json"),
            expression_hotkeys=hotkeys,
            queue_capacity=3,
        )
    )
    settings._paths = AppPaths(root=tmp_path / "AppData")

    sink = build_vts_event_sink(settings)

    assert sink is not None
    bridge = FakeVTSBridge.instances[0]
    assert bridge.started
    assert bridge.args[1].__class__.__name__ == "DPAPITokenStore"
    assert bridge.options["queue_capacity"] == 3
    asyncio.run(sink.close())
    assert bridge.closed


@pytest.mark.parametrize(
    "options",
    [
        {"uri": "http://127.0.0.1:8001"},
        {"reconnect_initial_seconds": 0.0},
        {"reconnect_initial_seconds": 2.0, "reconnect_max_seconds": 1.0},
        {"expression_hotkeys": {"happy": " "}},
    ],
)
def test_vts_settings_reject_invalid_network_bounds(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VTSConfig(**options)  # type: ignore[arg-type]
