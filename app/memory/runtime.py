"""Async composition boundary for synchronous private SQLite services."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime
from functools import partial
from threading import RLock
from typing import Any

from app.core import FeatureFlagSource, TurnObserver
from app.core.cancellation import CancellationToken
from app.emotion import Clock, SystemClock
from app.memory.analyzer import MemoryCandidateAnalyzer, NoopMemoryCandidateAnalyzer
from app.memory.context import ContextSnapshot, MemoryContextAssembler
from app.memory.models import MemoryItem, PendingConfirmation, SourceInputMode
from app.memory.service import HistoryService, MemoryService
from app.prompts import HistoryMessage, PromptContextSource
from app.schemas import (
    ExternalContextBlock,
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
    def __init__(self, assembler: MemoryContextAssembler) -> None:
        self._assembler = assembler
        self._snapshots: dict[str, ContextSnapshot] = {}
        self._lock = asyncio.Lock()

    async def history_for(self, message: UserMessage) -> Sequence[HistoryMessage]:
        snapshot = await self._build(message)
        async with self._lock:
            self._snapshots[message.message_id] = snapshot
        return snapshot.history

    async def context_for(self, message: UserMessage) -> Sequence[ExternalContextBlock]:
        async with self._lock:
            snapshot = self._snapshots.pop(message.message_id, None)
        if snapshot is None:
            snapshot = await self._build(message)
        return snapshot.blocks

    async def _build(self, message: UserMessage) -> ContextSnapshot:
        return await asyncio.to_thread(
            partial(
                self._assembler.build,
                user_id=message.user_id,
                session_id=message.session_id,
                query=message.text,
                exclude_message_id=message.message_id,
            )
        )


class MemoryTurnObserver(TurnObserver):
    """Persist explicit input at acceptance and assistant text only after success."""

    def __init__(
        self,
        history: HistoryService,
        memory: MemoryService,
        features: FeatureFlagManager,
        analyzer: MemoryCandidateAnalyzer,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._history = history
        self._memory = memory
        self._features = features
        self._analyzer = analyzer
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
        if not self._features.get(FeatureName.long_term_memory).enabled:
            return
        claims = await self._analyzer.analyze(
            message,
            CancellationToken(f"memory-{state.turn_id}"),
        )
        source_mode = SourceInputMode(message.input_mode.value)
        for claim in claims:
            await asyncio.to_thread(
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


class MemoryRuntime:
    def __init__(
        self,
        database: SQLiteDatabase,
        features: FeatureFlagManager,
        history: HistoryService,
        memory: MemoryService,
        memory_repository: MemoryRepository,
        analyzer: MemoryCandidateAnalyzer,
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
        self.context_source = context_source
        self.observer = observer
        self._clock = clock or SystemClock()
        self._stop = asyncio.Event()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name="memory-maintenance"
            )

    async def list_features(self) -> list[FeatureState]:
        return self.features.list()

    async def set_feature(self, name: FeatureName, enabled: bool) -> FeatureState:
        if name is FeatureName.long_term_memory:
            return await asyncio.to_thread(self.memory.set_enabled, enabled)
        return await asyncio.to_thread(
            self.features.set,
            name,
            enabled,
            updated_at=self._clock.now(),
        )

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
        return await asyncio.to_thread(
            partial(
                self.memory.update_for_management,
                memory_id,
                user_id=user_id,
                content=content,
            )
        )

    async def delete_memory(self, memory_id: str, *, user_id: str) -> bool:
        return await asyncio.to_thread(
            self.memory.delete_for_management, memory_id, user_id=user_id
        )

    async def confirm_memory(self, confirmation_id: str, *, approved: bool) -> MemoryItem | None:
        return await asyncio.to_thread(self.memory.confirm, confirmation_id, approved=approved)

    async def pending_confirmations(self) -> tuple[PendingConfirmation, ...]:
        return await asyncio.to_thread(self.memory.pending_confirmations)

    async def clear_memories(self, *, user_id: str) -> int:
        return await asyncio.to_thread(self.memory.clear_for_management, user_id=user_id)

    async def clear_history(self, *, user_id: str, session_id: str | None = None) -> int:
        return await asyncio.to_thread(
            self.history.clear_for_management, user_id=user_id, session_id=session_id
        )

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
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        task, self._maintenance_task = self._maintenance_task, None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await self.analyzer.close()
        await asyncio.to_thread(self.database.secure_cleanup)


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
    assembler = MemoryContextAssembler(history, memory)
    context_source = MemoryPromptContextSource(assembler)
    observer = MemoryTurnObserver(
        history,
        memory,
        features,
        resolved_analyzer,
        clock=clock,
    )
    runtime = MemoryRuntime(
        database,
        features,
        history,
        memory,
        memory_repository,
        resolved_analyzer,
        context_source,
        observer,
        clock=clock,
    )
    runtime.start()
    return runtime
