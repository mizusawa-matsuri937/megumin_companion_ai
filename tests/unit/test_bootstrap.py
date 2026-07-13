"""Dependency composition tests for disabled, mock, and real provider modes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.bootstrap import build_dialogue_pipeline, build_llm_provider, build_vts_event_sink
from app.clients.llm import MockLLMProvider, OpenAICompatibleLLMProvider
from app.clients.tts import GPTSoVITSProvider
from app.clients.vts import VTSBridgeSnapshot, VTSBridgeState
from app.config import Settings
from app.config.settings import (
    EmotionConfig,
    GPTSoVITSPresetConfig,
    LLMConfig,
    PipelineConfig,
    TTSConfig,
    VTSConfig,
)
from app.pipelines.audio_player import SystemAudioPlayer
from app.prompts import EmotionPromptContextBuilder, HistoryMessage, PromptContextSnapshot
from app.schemas import ChatRole, ExternalContextBlock, UserMessage
from app.schemas.ai import ContextOrigin, ContextTrust


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


def test_real_provider_absolute_cache_and_system_player(tmp_path: Path) -> None:
    settings = Settings(
        llm=LLMConfig(
            provider="compatible",
            model="test-model",
            api_key_env="TEST_KEY",
            temperature=0.65,
            max_tokens=777,
        ),
        pipeline=PipelineConfig(audio_cache_path=tmp_path, playback_mode="system"),
    )
    settings._environment = {"TEST_KEY": "fake-test-key"}

    pipeline = build_dialogue_pipeline(settings)

    assert pipeline is not None
    assert isinstance(pipeline._audio_player, SystemAudioPlayer)
    assert isinstance(pipeline._llm, OpenAICompatibleLLMProvider)
    assert pipeline._llm._default_temperature == 0.65
    assert pipeline._llm._default_max_tokens == 777
    asyncio.run(pipeline.close())


def test_gpt_sovits_wiring_is_explicit_and_cache_defaults_off(tmp_path: Path) -> None:
    settings = Settings(
        llm=LLMConfig(provider="mock"),
        tts=TTSConfig(
            provider="gpt-sovits",
            output_directory=tmp_path / "ephemeral",
            cache_directory=tmp_path / "persistent",
            presets={"default": GPTSoVITSPresetConfig(ref_audio_path="/local/reference.wav")},
        ),
    )

    pipeline = build_dialogue_pipeline(settings)

    assert pipeline is not None
    assert isinstance(pipeline._tts, GPTSoVITSProvider)
    assert not pipeline._tts._cache_enabled
    asyncio.run(pipeline.close())


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
            self.options = options
            self.started = False
            self.closed = False
            self.instances.append(self)

        def start(self) -> None:
            self.started = True

        def enqueue_expression(self, _expression: str, *, turn_id: str | None = None) -> bool:
            return turn_id is not None

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
            token_path=tmp_path / "token.json",
            expression_hotkeys=hotkeys,
            queue_capacity=3,
        )
    )

    sink = build_vts_event_sink(settings)

    assert sink is not None
    bridge = FakeVTSBridge.instances[0]
    assert bridge.started
    assert bridge.options["queue_capacity"] == 3
    asyncio.run(sink.close())
    assert bridge.closed


@pytest.mark.parametrize(
    "options",
    [
        {"uri": "http://127.0.0.1:8001"},
        {"reconnect_initial_seconds": 2.0, "reconnect_max_seconds": 1.0},
    ],
)
def test_vts_settings_reject_invalid_network_bounds(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VTSConfig(**options)  # type: ignore[arg-type]
