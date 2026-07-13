"""Supervised memory-candidate scheduling and shutdown boundaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core import CancellationToken, TurnService
from app.memory import MemoryClaim, MemorySensitivity, MemoryType
from app.memory.runtime import create_memory_runtime
from app.schemas import (
    FeatureName,
    TurnMetrics,
    TurnOutcome,
    TurnState,
    UserMessage,
)


class GatedAnalyzer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.close_calls = 0

    async def analyze(
        self,
        _message: UserMessage,
        token: CancellationToken,
    ) -> list[MemoryClaim]:
        self.started.set()
        try:
            await self.release.wait()
            token.raise_if_cancelled()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return [
            MemoryClaim(
                memory_type=MemoryType.fact,
                canonical_key="preference:coffee",
                content="用户喜欢手冲咖啡",
                evidence_quote="我喜欢手冲咖啡",
                importance_score=0.9,
                confidence_score=0.95,
                sensitivity_hint=MemorySensitivity.normal,
            )
        ]

    async def close(self) -> None:
        self.close_calls += 1


class ImmediatePipeline:
    def __init__(self) -> None:
        self.closed = False

    async def run(
        self,
        _message: UserMessage,
        _state: TurnState,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        token.raise_if_cancelled()
        await emit("assistant.delta", {"delta": "知道了"})
        return TurnOutcome(
            full_text="知道了",
            metrics=TurnMetrics(turn_total_ms=1),
        )

    async def close(self) -> None:
        self.closed = True


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.memory.runtime")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


async def _receive_completed(queue: asyncio.Queue[Any]) -> None:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=1)
        queue.task_done()
        if event.type == "assistant.completed":
            return


def test_candidate_llm_does_not_block_assistant_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        analyzer = GatedAnalyzer()
        runtime = await create_memory_runtime(str(tmp_path / "memory.sqlite3"), analyzer=analyzer)
        await runtime.set_feature(FeatureName.long_term_memory, True)
        pipeline = ImmediatePipeline()
        service = TurnService(_logger(), pipeline, observers=(runtime.observer,))
        queue = service.subscribe("local_session")

        await service.accept(UserMessage(text="我喜欢手冲咖啡"))
        await _receive_completed(queue)
        await analyzer.started.wait()

        assert not analyzer.release.is_set()
        assert await runtime.list_memories(user_id="local_user") == []

        analyzer.release.set()
        await runtime.candidates.wait_idle()
        memories = await runtime.list_memories(user_id="local_user")
        assert [item.content for item in memories] == ["用户喜欢手冲咖啡"]

        await service.shutdown()
        await asyncio.gather(runtime.close(), runtime.close())
        assert pipeline.closed
        assert analyzer.close_calls == 1

    asyncio.run(scenario())


def test_disabling_long_term_memory_cancels_and_joins_active_analysis(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        analyzer = GatedAnalyzer()
        runtime = await create_memory_runtime(str(tmp_path / "disabled.sqlite3"), analyzer=analyzer)
        await runtime.set_feature(FeatureName.long_term_memory, True)
        service = TurnService(_logger(), ImmediatePipeline(), observers=(runtime.observer,))
        queue = service.subscribe("local_session")

        await service.accept(UserMessage(text="我喜欢手冲咖啡"))
        await _receive_completed(queue)
        await analyzer.started.wait()
        disabled = await runtime.set_feature(FeatureName.long_term_memory, False)

        assert not disabled.enabled
        assert analyzer.cancelled.is_set()
        assert runtime.candidates._tasks == {}
        assert await runtime.list_memories(user_id="local_user") == []

        await service.shutdown()
        await runtime.close()
        assert analyzer.close_calls == 1

    asyncio.run(scenario())
