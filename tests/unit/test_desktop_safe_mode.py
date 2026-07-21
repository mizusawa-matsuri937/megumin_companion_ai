from __future__ import annotations

import asyncio
from pathlib import Path

from app.config.settings import LLMConfig, LoggingConfig, Settings, StorageConfig
from app.main import create_app
from app.memory.runtime import MemoryRuntime
from app.paths import AppPaths
from app.schemas import FeatureName


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
