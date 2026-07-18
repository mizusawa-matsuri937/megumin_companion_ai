"""Supervised memory-candidate scheduling and shutdown boundaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any, Literal

import pytest
from app.core import CancellationToken, TurnService
from app.memory import (
    ContextSnapshot,
    MemoryClaim,
    MemoryContextAssembler,
    MemoryItem,
    MemorySensitivity,
    MemoryType,
    SourceInputMode,
)
from app.memory.runtime import MemoryRuntime, _drainable_to_thread, create_memory_runtime
from app.schemas import (
    FeatureName,
    FeatureState,
    TurnMetrics,
    TurnOutcome,
    TurnState,
    UserMessage,
)
from app.storage import ConversationOrigin, ConversationRecord, ConversationRole


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


class BlockingSnapshotAssembler(MemoryContextAssembler):
    """Return one pre-mutation snapshot late, then delegate all retries normally."""

    def __init__(self, delegate: MemoryContextAssembler) -> None:
        self._delegate = delegate
        self.entered = Event()
        self.release = Event()
        self._calls_lock = Lock()
        self.calls = 0

    def build(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        exclude_message_id: str | None = None,
        history_limit: int = 100,
        memory_limit: int = 10,
    ) -> ContextSnapshot:
        snapshot = self._delegate.build(
            user_id=user_id,
            session_id=session_id,
            query=query,
            exclude_message_id=exclude_message_id,
            history_limit=history_limit,
            memory_limit=memory_limit,
        )
        with self._calls_lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.entered.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test did not release stale context snapshot")
        return snapshot


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.memory.runtime")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


async def _receive_completed(queue: Any) -> None:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=1)
        queue.task_done()
        if event.type == "assistant.completed":
            return


def _save_memory(runtime: MemoryRuntime, *, suffix: str = "手冲") -> MemoryItem:
    source = f"我喜欢{suffix}咖啡"
    result = runtime.memory.consider_user_claim(
        MemoryClaim(
            memory_type=MemoryType.fact,
            canonical_key="preference:coffee",
            content=f"用户喜欢{suffix}咖啡",
            evidence_quote=source,
            importance_score=0.9,
            confidence_score=0.95,
            sensitivity_hint=MemorySensitivity.normal,
        ),
        user_id="local_user",
        source_message_id=f"seed-{suffix}",
        source_input_mode=SourceInputMode.text,
        source_text=source,
        created_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
    )
    assert result.item is not None
    return result.item


def _block_first_snapshot(runtime: MemoryRuntime) -> BlockingSnapshotAssembler:
    blocker = BlockingSnapshotAssembler(runtime.context_source._assembler)
    runtime.context_source._assembler = blocker
    return blocker


async def _wait_for_thread_event(event: Event) -> None:
    assert await asyncio.to_thread(event.wait, 1)


def test_candidate_llm_does_not_block_assistant_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        analyzer = GatedAnalyzer()
        runtime = await create_memory_runtime(str(tmp_path / "memory.sqlite3"), analyzer=analyzer)
        await runtime.set_feature(FeatureName.long_term_memory, True)
        pipeline = ImmediatePipeline()
        service = TurnService(_logger(), pipeline, observers=(runtime.observer,))
        queue = await service.subscribe("local_session")

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
        queue = await service.subscribe("local_session")

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


def test_repeated_cancellation_cannot_release_an_active_sqlite_worker() -> None:
    async def scenario() -> None:
        entered = Event()
        release = Event()

        def blocking_worker() -> str:
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release worker")
            return "finished"

        task = asyncio.create_task(_drainable_to_thread(blocking_worker))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_memory_update_retries_an_inflight_pre_mutation_snapshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await create_memory_runtime(str(tmp_path / "update-context.sqlite3"))
        await runtime.set_feature(FeatureName.long_term_memory, True)
        item = _save_memory(runtime)
        blocker = _block_first_snapshot(runtime)
        pending = asyncio.create_task(runtime.context_source.snapshot_for(UserMessage(text="咖啡")))
        try:
            await _wait_for_thread_event(blocker.entered)
            updated = await runtime.update_memory(
                item.memory_id,
                user_id="local_user",
                content="用户喜欢深烘咖啡",
            )
            assert updated is not None
            assert not pending.done()

            blocker.release.set()
            snapshot = await pending
            serialized = "\n".join(block.content for block in snapshot.blocks)
            assert "用户喜欢深烘咖啡" in serialized
            assert "用户喜欢手冲咖啡" not in serialized
            assert blocker.calls == 2
        finally:
            blocker.release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["delete", "clear", "disable"])
def test_memory_revocation_retries_an_inflight_snapshot_without_old_blocks(
    tmp_path: Path,
    operation: Literal["delete", "clear", "disable"],
) -> None:
    async def scenario() -> None:
        runtime = await create_memory_runtime(str(tmp_path / f"{operation}-context.sqlite3"))
        await runtime.set_feature(FeatureName.long_term_memory, True)
        item = _save_memory(runtime)
        blocker = _block_first_snapshot(runtime)
        pending = asyncio.create_task(runtime.context_source.snapshot_for(UserMessage(text="咖啡")))
        try:
            await _wait_for_thread_event(blocker.entered)
            if operation == "delete":
                assert await runtime.delete_memory(item.memory_id, user_id="local_user")
            elif operation == "clear":
                assert await runtime.clear_memories(user_id="local_user") == 1
            else:
                assert not (await runtime.set_feature(FeatureName.long_term_memory, False)).enabled
            assert not pending.done()

            blocker.release.set()
            snapshot = await pending
            assert snapshot.blocks == ()
            assert blocker.calls == 2
        finally:
            blocker.release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["clear", "disable"])
def test_history_revocation_retries_an_inflight_snapshot_without_old_history(
    tmp_path: Path,
    operation: Literal["clear", "disable"],
) -> None:
    async def scenario() -> None:
        runtime = await create_memory_runtime(str(tmp_path / f"history-{operation}.sqlite3"))
        record = ConversationRecord(
            message_id="old-history",
            session_id="local_session",
            user_id="local_user",
            turn_id="old-turn",
            role=ConversationRole.user,
            origin=ConversationOrigin.user_text,
            content="应被撤销的旧历史",
            created_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
        )
        assert runtime.history.record(record)
        blocker = _block_first_snapshot(runtime)
        pending = asyncio.create_task(
            runtime.context_source.snapshot_for(UserMessage(text="新消息"))
        )
        try:
            await _wait_for_thread_event(blocker.entered)
            if operation == "clear":
                assert (
                    await runtime.clear_history(
                        user_id="local_user",
                        session_id="local_session",
                    )
                    == 1
                )
            else:
                assert not (await runtime.set_feature(FeatureName.recent_history, False)).enabled
            assert not pending.done()

            blocker.release.set()
            snapshot = await pending
            assert snapshot.history == ()
            assert blocker.calls == 2
        finally:
            blocker.release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await runtime.close()

    asyncio.run(scenario())


def test_feature_patch_waits_for_registered_privacy_transition(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await create_memory_runtime(str(tmp_path / "barrier.sqlite3"))
        entered = asyncio.Event()
        release = asyncio.Event()
        observed: list[tuple[FeatureName, bool]] = []

        async def barrier(state: FeatureState) -> None:
            entered.set()
            await release.wait()
            observed.append((state.name, state.enabled))

        unsubscribe = runtime.add_feature_transition_handler(barrier)
        patch = asyncio.create_task(runtime.set_feature(FeatureName.vision, False))
        await entered.wait()
        assert not patch.done()
        release.set()
        state = await patch
        assert not state.enabled
        assert observed == [(FeatureName.vision, False)]
        unsubscribe()
        await runtime.close()

    asyncio.run(scenario())
