"""Async composition boundary for synchronous private SQLite services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from functools import partial
from threading import RLock
from typing import Any

from app.core import FeatureFlagSource, TurnObserver
from app.core.cancellation import CancellationToken
from app.emotion import Clock, SystemClock
from app.memory.analyzer import MemoryCandidateAnalyzer, NoopMemoryCandidateAnalyzer
from app.memory.context import MemoryContextAssembler
from app.memory.models import MemoryItem, PendingConfirmation, SourceInputMode
from app.memory.service import HistoryService, MemoryService
from app.prompts import PromptContextSnapshot, PromptContextSource
from app.schemas import (
    FeatureName,
    FeatureState,
    TurnOutcome,
    TurnState,
    UserMessage,
    utc_now,
)
from app.storage import (
    ConversationOrigin,
    ConversationRecord,
    ConversationRepository,
    ConversationRole,
    FeatureFlagRepository,
    MemoryRepository,
    SQLiteDatabase,
)


class FeatureFlagManager(FeatureFlagSource):
    """Thread-safe snapshot so disabled features cause zero repository access."""

    def __init__(self, repository: FeatureFlagRepository) -> None:
        self._repository = repository
        self._states = {state.name: state for state in repository.list()}
        self._listeners: set[Callable[[FeatureState], None]] = set()
        self._lock = RLock()

    def get(self, name: FeatureName) -> FeatureState:
        with self._lock:
            return self._states[name]

    def get_feature(self, name: FeatureName) -> FeatureState:
        return self.get(name)

    def list(self) -> list[FeatureState]:
        with self._lock:
            return [self._states[name] for name in sorted(self._states, key=str)]

    def set(self, name: FeatureName, enabled: bool, *, updated_at: datetime) -> FeatureState:
        state = self._repository.set(name, enabled, updated_at=updated_at)
        with self._lock:
            self._states[name] = state
            listeners = tuple(self._listeners)
        for listener in listeners:
            with suppress(Exception):
                listener(state)
        return state

    def subscribe(self, listener: Callable[[FeatureState], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe


class MemoryPromptContextSource(PromptContextSource):
    """Build one cache-free context snapshot and retry across every revocation epoch."""

    def __init__(self, assembler: MemoryContextAssembler) -> None:
        self._assembler = assembler
        self._epoch = 0
        self._lock = asyncio.Lock()

    async def snapshot_for(self, message: UserMessage) -> PromptContextSnapshot:
        while True:
            async with self._lock:
                epoch = self._epoch
            snapshot = await self._build(message)
            async with self._lock:
                if epoch == self._epoch:
                    return snapshot

    async def invalidate(self) -> None:
        """Make every snapshot built before this barrier ineligible for future prompts."""

        async with self._lock:
            self._epoch += 1

    async def _build(self, message: UserMessage) -> PromptContextSnapshot:
        snapshot = await asyncio.to_thread(
            partial(
                self._assembler.build,
                user_id=message.user_id,
                session_id=message.session_id,
                query=message.text,
                exclude_message_id=message.message_id,
            )
        )
        return PromptContextSnapshot(history=snapshot.history, blocks=snapshot.blocks)


class MemoryCandidateSupervisor:
    """Run best-effort LLM extraction outside turn completion's critical path."""

    def __init__(
        self,
        memory: MemoryService,
        features: FeatureFlagManager,
        analyzer: MemoryCandidateAnalyzer,
    ) -> None:
        self._memory = memory
        self._features = features
        self._analyzer = analyzer
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[asyncio.Task[None], CancellationToken] = {}
        self._cancellation_started: set[asyncio.Task[None]] = set()
        self._unsubscribe: Callable[[], None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None:
            if self._loop is not loop:
                raise RuntimeError("MemoryCandidateSupervisor 不能跨 event loop 使用")
            return
        self._loop = loop
        self._unsubscribe = self._features.subscribe(self._on_feature_changed)

    def submit(self, message: UserMessage, state: TurnState) -> bool:
        loop = self._loop
        if (
            self._closed
            or loop is None
            or not self._features.get(FeatureName.long_term_memory).enabled
        ):
            return False
        token = CancellationToken(f"memory-{state.turn_id}")
        task = loop.create_task(
            self._analyze_and_store(message, token),
            name=f"memory-candidate-{state.turn_id}",
        )
        self._tasks[task] = token
        task.add_done_callback(self._task_done)
        return True

    async def cancel_active(self) -> None:
        self._cancel_all_now()
        await self.wait_idle()

    async def wait_idle(self) -> None:
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )

    async def close(self) -> None:
        task = self._close_task
        if task is None:
            task = asyncio.create_task(self._close_impl(), name="memory-candidate-supervisor-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close_impl(self) -> None:
        self._closed = True
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            with suppress(Exception):
                unsubscribe()
        self._cancel_all_now()
        await self.wait_idle()

    async def _analyze_and_store(
        self,
        message: UserMessage,
        token: CancellationToken,
    ) -> None:
        try:
            claims = await self._analyzer.analyze(message, token)
            token.raise_if_cancelled()
            source_mode = SourceInputMode(message.input_mode.value)
            for claim in claims:
                token.raise_if_cancelled()
                if not self._features.get(FeatureName.long_term_memory).enabled:
                    return
                await _drainable_to_thread(
                    partial(
                        self._memory.consider_user_claim,
                        claim,
                        user_id=message.user_id,
                        source_message_id=message.message_id,
                        source_input_mode=source_mode,
                        source_text=message.text,
                        created_at=message.created_at,
                    )
                )
        except asyncio.CancelledError:
            token.cancel()
            raise
        except Exception:
            # Candidate analysis is optional and must not fail or expose a turn.
            return

    def _on_feature_changed(self, state: FeatureState) -> None:
        if state.name is not FeatureName.long_term_memory or state.enabled:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(self._cancel_all_now)

    def _cancel_all_now(self) -> None:
        for task, token in tuple(self._tasks.items()):
            if task in self._cancellation_started or task.done():
                continue
            self._cancellation_started.add(task)
            token.cancel()
            task.cancel()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.pop(task, None)
        self._cancellation_started.discard(task)
        if not task.cancelled():
            with suppress(asyncio.CancelledError):
                task.exception()


class MemoryTurnObserver(TurnObserver):
    """Persist explicit input at acceptance and assistant text only after success."""

    def __init__(
        self,
        history: HistoryService,
        candidates: MemoryCandidateSupervisor,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._history = history
        self._candidates = candidates
        self._clock = clock or SystemClock()

    async def on_user_accepted(self, message: UserMessage, state: TurnState) -> None:
        origin = (
            ConversationOrigin.user_voice
            if message.input_mode.value == "voice"
            else ConversationOrigin.user_text
        )
        await asyncio.to_thread(
            self._history.record,
            ConversationRecord(
                message_id=message.message_id,
                session_id=message.session_id,
                user_id=message.user_id,
                turn_id=state.turn_id,
                role=ConversationRole.user,
                origin=origin,
                content=message.text,
                created_at=message.created_at,
            ),
        )

    async def on_turn_completed(
        self,
        message: UserMessage,
        state: TurnState,
        outcome: TurnOutcome,
    ) -> None:
        if outcome.full_text.strip():
            await asyncio.to_thread(
                self._history.record,
                ConversationRecord(
                    message_id=f"assistant_{state.turn_id}",
                    session_id=message.session_id,
                    user_id=message.user_id,
                    turn_id=state.turn_id,
                    role=ConversationRole.assistant,
                    origin=ConversationOrigin.assistant_dialogue,
                    content=outcome.full_text,
                    created_at=self._clock.now(),
                ),
            )
        self._candidates.submit(message, state)


class MemoryRuntime:
    def __init__(
        self,
        database: SQLiteDatabase,
        features: FeatureFlagManager,
        history: HistoryService,
        memory: MemoryService,
        memory_repository: MemoryRepository,
        analyzer: MemoryCandidateAnalyzer,
        candidates: MemoryCandidateSupervisor,
        context_source: MemoryPromptContextSource,
        observer: MemoryTurnObserver,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.database = database
        self.features = features
        self.history = history
        self.memory = memory
        self.memory_repository = memory_repository
        self.analyzer = analyzer
        self.candidates = candidates
        self.context_source = context_source
        self.observer = observer
        self._clock = clock or SystemClock()
        self._stop = asyncio.Event()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._feature_update_lock = asyncio.Lock()
        self._feature_transition_handlers: set[Callable[[FeatureState], Awaitable[None]]] = set()

    def start(self) -> None:
        self.candidates.start()
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name="memory-maintenance"
            )

    async def list_features(self) -> list[FeatureState]:
        return self.features.list()

    def add_feature_transition_handler(
        self,
        handler: Callable[[FeatureState], Awaitable[None]],
    ) -> Callable[[], None]:
        self._feature_transition_handlers.add(handler)

        def unsubscribe() -> None:
            self._feature_transition_handlers.discard(handler)

        return unsubscribe

    async def set_feature(self, name: FeatureName, enabled: bool) -> FeatureState:
        async with self._feature_update_lock:
            if name is FeatureName.long_term_memory:
                state = await asyncio.to_thread(self.memory.set_enabled, enabled)
                if not enabled:
                    await self.candidates.cancel_active()
                    await asyncio.to_thread(self.memory.finalize_disabled_state)
                await self.context_source.invalidate()
            elif name is FeatureName.recent_history:
                state = await asyncio.to_thread(self.history.set_enabled, enabled)
                await self.context_source.invalidate()
            else:
                state = await asyncio.to_thread(
                    self.features.set,
                    name,
                    enabled,
                    updated_at=self._clock.now(),
                )
            for handler in tuple(self._feature_transition_handlers):
                await handler(state)
            return state

    async def list_memories(
        self, *, user_id: str, include_superseded: bool = False
    ) -> list[MemoryItem]:
        return await asyncio.to_thread(
            partial(
                self.memory.list_for_management,
                user_id=user_id,
                include_superseded=include_superseded,
            )
        )

    async def search_memories(
        self, *, user_id: str, query: str, limit: int = 10
    ) -> list[MemoryItem]:
        return await asyncio.to_thread(
            partial(self.memory_repository.search, user_id=user_id, query=query, limit=limit)
        )

    async def update_memory(
        self, memory_id: str, *, user_id: str, content: str
    ) -> MemoryItem | None:
        item = await asyncio.to_thread(
            partial(
                self.memory.update_for_management,
                memory_id,
                user_id=user_id,
                content=content,
            )
        )
        if item is not None:
            await self.context_source.invalidate()
        return item

    async def delete_memory(self, memory_id: str, *, user_id: str) -> bool:
        deleted = await asyncio.to_thread(
            self.memory.delete_for_management, memory_id, user_id=user_id
        )
        if deleted:
            await self.context_source.invalidate()
        return deleted

    async def confirm_memory(self, confirmation_id: str, *, approved: bool) -> MemoryItem | None:
        item = await asyncio.to_thread(self.memory.confirm, confirmation_id, approved=approved)
        if item is not None:
            await self.context_source.invalidate()
        return item

    async def pending_confirmations(self) -> tuple[PendingConfirmation, ...]:
        return await asyncio.to_thread(self.memory.pending_confirmations)

    async def clear_memories(self, *, user_id: str) -> int:
        deleted = await asyncio.to_thread(self.memory.clear_for_management, user_id=user_id)
        await self.context_source.invalidate()
        return deleted

    async def clear_history(self, *, user_id: str, session_id: str | None = None) -> int:
        deleted = await asyncio.to_thread(
            self.history.clear_for_management, user_id=user_id, session_id=session_id
        )
        await self.context_source.invalidate()
        return deleted

    async def export(self, *, user_id: str) -> dict[str, Any]:
        items = await self.list_memories(user_id=user_id, include_superseded=True)
        sources = {
            item.memory_id: await asyncio.to_thread(
                self.memory_repository.list_sources, item.memory_id
            )
            for item in items
        }
        return {
            "exported_at": utc_now().isoformat(),
            "user_id": user_id,
            "features": [state.model_dump(mode="json") for state in self.features.list()],
            "memories": [item.model_dump(mode="json") for item in items],
            "sources": {
                memory_id: [source.model_dump(mode="json") for source in records]
                for memory_id, records in sources.items()
            },
        }

    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
            except TimeoutError:
                await asyncio.to_thread(self.memory.prune_expired_confirmations)
                await asyncio.to_thread(self.history.cleanup)

    async def close(self) -> None:
        task = self._close_task
        if task is None:
            task = asyncio.create_task(self._close_impl(), name="memory-runtime-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close_impl(self) -> None:
        self._closed = True
        self._stop.set()
        task, self._maintenance_task = self._maintenance_task, None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        errors: list[Exception] = []
        for closer in (self.candidates.close, self.analyzer.close):
            try:
                await closer()
            except Exception as exc:
                errors.append(exc)
        try:
            await asyncio.to_thread(self.database.secure_cleanup)
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise ExceptionGroup("memory runtime resource close failed", errors)


async def create_memory_runtime(
    database_path: str,
    *,
    busy_timeout_ms: int = 5_000,
    retention_days: int = 7,
    confirmation_ttl_minutes: float = 15.0,
    analyzer: MemoryCandidateAnalyzer | None = None,
) -> MemoryRuntime:
    from datetime import timedelta

    database = SQLiteDatabase(database_path, busy_timeout_ms=busy_timeout_ms)
    await asyncio.to_thread(database.initialize)
    features = await asyncio.to_thread(FeatureFlagManager, FeatureFlagRepository(database))
    clock = SystemClock()
    history = HistoryService(
        ConversationRepository(database),
        features,
        clock=clock,
        retention_days=retention_days,
    )
    memory_repository = MemoryRepository(database)
    memory = MemoryService(
        memory_repository,
        features,
        clock=clock,
        confirmation_ttl=timedelta(minutes=confirmation_ttl_minutes),
    )
    resolved_analyzer = analyzer or NoopMemoryCandidateAnalyzer()
    candidates = MemoryCandidateSupervisor(memory, features, resolved_analyzer)
    assembler = MemoryContextAssembler(history, memory)
    context_source = MemoryPromptContextSource(assembler)
    observer = MemoryTurnObserver(
        history,
        candidates,
        clock=clock,
    )
    runtime = MemoryRuntime(
        database,
        features,
        history,
        memory,
        memory_repository,
        resolved_analyzer,
        candidates,
        context_source,
        observer,
        clock=clock,
    )
    runtime.start()
    return runtime


async def _drainable_to_thread(operation: Callable[[], Any]) -> Any:
    """Do not release private inputs while their worker still owns the closure."""

    worker = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            cancelled = True
            if worker.done():
                await asyncio.gather(worker, return_exceptions=True)
                raise
