from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.api.security import DevAPIConfig
from app.clients.llm import DeepSeekFlashLLMProvider
from app.config.settings import LLMConfig, LoggingConfig, Settings, StorageConfig
from app.main import create_app
from app.memory.runtime import MemoryRuntime
from app.paths import AppPaths
from app.perception.prompt_context import (
    ApprovedVisualSummary,
    PerceptionPromptContextSource,
    VisualSummaryLabel,
)
from app.schemas import FeatureName, PerceptionContext, UserMessage, utc_now


def _settings(root: Path) -> Settings:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=True, database_path=Path("companion.sqlite3")),
        llm=LLMConfig(provider="mock"),
    )
    settings._paths = AppPaths(root=root)
    return settings


def test_crash_safe_mode_persists_sensitive_features_disabled(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        ordinary = create_app(settings)
        async with ordinary.router.lifespan_context(ordinary):
            runtime = ordinary.state.memory_runtime
            assert isinstance(runtime, MemoryRuntime)
            for feature in (FeatureName.vision, FeatureName.cloud_vision, FeatureName.proactive):
                assert (await runtime.set_feature(feature, True)).enabled

        recovered = create_app(settings, safe_mode=True)
        async with recovered.router.lifespan_context(recovered):
            runtime = recovered.state.memory_runtime
            assert isinstance(runtime, MemoryRuntime)
            for feature in (FeatureName.vision, FeatureName.cloud_vision, FeatureName.proactive):
                state = runtime.features.get(feature)
                assert not state.desired_enabled
                assert not state.enabled

    asyncio.run(scenario())


def test_deepseek_startup_refuses_memory_candidate_analysis_before_provider_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path / "MeguminCompanion")
        settings.llm = settings.llm.model_copy(update={"provider": "deepseek"})
        settings.memory = settings.memory.model_copy(update={"candidate_analysis_enabled": True})
        provider_build_attempted = False

        def unexpected_provider_build(_settings: Settings) -> None:
            nonlocal provider_build_attempted
            provider_build_attempted = True
            raise AssertionError("DeepSeek Flash candidate analyzer must not be constructed")

        monkeypatch.setattr("app.main.build_llm_provider", unexpected_provider_build)
        application = create_app(settings)
        with pytest.raises(RuntimeError, match="DeepSeek Flash"):
            async with application.router.lifespan_context(application):
                pass
        assert not provider_build_attempted

    asyncio.run(scenario())


def test_deepseek_environment_key_is_available_only_to_explicit_non_desktop_runtime(
    tmp_path: Path,
) -> None:
    def settings_for(root: Path) -> Settings:
        settings = Settings(
            logging=LoggingConfig(console_enabled=False, file_enabled=False),
            storage=StorageConfig(enabled=False, database_path=Path("companion.sqlite3")),
            llm=LLMConfig(provider="deepseek"),
        )
        settings._environment = {"DEEPSEEK_API_KEY": "non-desktop-fallback-key"}
        settings._paths = AppPaths(root=root)
        return settings

    async def scenario() -> None:
        desktop = create_app(settings_for(tmp_path / "desktop"))
        with pytest.raises(RuntimeError, match="deepseek-api-key"):
            async with desktop.router.lifespan_context(desktop):
                pass

        non_desktop = create_app(
            settings_for(tmp_path / "dev-api"),
            dev_api=DevAPIConfig.generate(host="127.0.0.1", port=8765),
            allow_deepseek_env_fallback=True,
        )
        async with non_desktop.router.lifespan_context(non_desktop):
            service = non_desktop.state.turn_service
            assert service is not None
            pipeline = service._pipeline
            assert pipeline is not None
            assert isinstance(pipeline._llm, DeepSeekFlashLLMProvider)
            assert (
                pipeline._llm._client.headers["Authorization"] == "Bearer non-desktop-fallback-key"
            )

    asyncio.run(scenario())


def test_deepseek_environment_fallback_requires_the_explicit_dev_api_surface(
    tmp_path: Path,
) -> None:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=False, database_path=Path("companion.sqlite3")),
        llm=LLMConfig(provider="deepseek"),
    )
    settings._paths = AppPaths(root=tmp_path / "MeguminCompanion")

    with pytest.raises(ValueError, match="--dev-api"):
        create_app(settings, allow_deepseek_env_fallback=True)


def test_app_composes_only_approved_visual_summaries_and_never_forwards_identifiers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        application = create_app(_settings(tmp_path / "MeguminCompanion"))
        async with application.router.lifespan_context(application):
            runtime = application.state.memory_runtime
            source = application.state.perception_context_source
            publish_raw_perception = application.state.publish_perception
            publish_approved_summary = application.state.publish_approved_visual_summary
            service = application.state.turn_service
            assert isinstance(runtime, MemoryRuntime)
            assert isinstance(source, PerceptionPromptContextSource)
            assert callable(publish_raw_perception)
            assert callable(publish_approved_summary)
            assert service is not None
            assert (await runtime.set_feature(FeatureName.vision, True)).enabled

            await publish_raw_perception(
                PerceptionContext(
                    observation_id="https://example.invalid/capture.png?ocr=raw-ocr-sentinel",
                    summary="raw-ocr-sentinel",
                    observed_at=utc_now(),
                )
            )
            pipeline = service._pipeline
            assert pipeline is not None
            request = await pipeline._context_builder.build(
                UserMessage(text="请结合可用摘要回复", screen_context_allowed=True)
            )
            serialized = "\n".join(str(message.content) for message in request.messages)
            assert "raw-ocr-sentinel" not in serialized
            assert "example.invalid/capture.png" not in serialized

            await publish_approved_summary(
                ApprovedVisualSummary(
                    labels=(VisualSummaryLabel.non_sensitive_change,),
                    observed_at=utc_now(),
                )
            )
            request = await pipeline._context_builder.build(
                UserMessage(text="请结合可用摘要回复", screen_context_allowed=True)
            )
            serialized = "\n".join(str(message.content) for message in request.messages)
            assert "检测到非敏感视觉变化。" in serialized
            assert "raw-ocr-sentinel" not in serialized
            assert "example.invalid/capture.png" not in serialized

            assert not (await runtime.set_feature(FeatureName.vision, False)).enabled
            request = await pipeline._context_builder.build(
                UserMessage(text="视觉关闭后不应使用摘要", screen_context_allowed=True)
            )
            assert "检测到非敏感视觉变化。" not in "\n".join(
                str(message.content) for message in request.messages
            )

    asyncio.run(scenario())
