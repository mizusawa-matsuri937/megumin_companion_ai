"""Dependency composition tests for disabled, mock, and real provider modes."""

import asyncio
from pathlib import Path

import pytest
from app.bootstrap import build_dialogue_pipeline
from app.clients.llm import OpenAICompatibleLLMProvider
from app.config import Settings
from app.config.settings import LLMConfig, PipelineConfig
from app.pipelines.audio_player import SystemAudioPlayer


def test_disabled_provider_builds_no_pipeline() -> None:
    assert build_dialogue_pipeline(Settings(llm=LLMConfig(provider=" NONE "))) is None


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
