"""Async composition boundary for synchronous private SQLite services."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from threading import RLock
from typing import Any

from app.core import FeatureFlagSource, TurnObserver
from app.core.cancellation import CancellationToken
from app.emotion import Clock, SystemClock
from app.health import CapabilityCheck, CapabilityState
from app.memory.analyzer import MemoryCandidateAnalyzer, NoopMemoryCandidateAnalyzer
from app.memory.context import MemoryContextAssembler
from app.memory.models import MemoryItem, PendingConfirmation, SourceInputMode
from app.memory.service import HistoryService, MemoryService
from app.prompts import PromptContextSnapshot, PromptContextSource
from app.schemas import (
    FeatureActualState,
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
    DatabaseSafeModeError,
    FeatureFlagRepository,
    MemoryRepository,
    MigrationError,
    PhysicalCleanupRepository,
    SQLiteDatabase,
)
from app.storage.records import CleanupJob, DeletionResult

_CLEANUP_ERROR_CODES = {"db_busy", "db_full", "db_corrupt", "file_locked"}


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
        self._publish(state)
        return state

    def request_transition(
        self, name: FeatureName, enabled: bool, *, updated_at: datetime
    ) -> FeatureState:
        state = self._repository.request_transition(name, enabled, updated_at=updated_at)
        with self._lock:
            self._states[name] = state
        return state

    def finish_transition(
        self,
        name: FeatureName,
        *,
        generation: int,
        actual_state: FeatureActualState,
        reason_code: str | None,
        updated_at: datetime,
    ) -> FeatureState:
        state = self._repository.finish_transition(
            name,
            generation=generation,
            actual_state=actual_state,
            reason_code=reason_code,
            updated_at=updated_at,
        )
        self._publish(state)
        return state

    def _publish(self, state: FeatureState) -> None:
        with self._lock:
            self._states[state.name] = state
            listeners = tuple(self._listeners)
        for listener in listeners:
            with suppress(Exception):
                listener(state)

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


@dataclass(frozen=True, slots=True)
class MaintenanceSnapshot:
    consecutive_failures: int
    next_delay_seconds: float
    error_codes: tuple[str, ...]


class _MaintenanceFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SafeModeMemoryRuntime:
    """Content-free application boundary when private SQLite cannot be written safely."""

    name = "memory"
    required_for_readiness = False

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def check_health(self) -> CapabilityCheck:
        return CapabilityCheck(
            status=CapabilityState.unavailable,
            error_code=self.database.status.reason_code or "database_safe_mode",
        )

    async def recovery_status(self) -> dict[str, Any]:
        status = self.database.status
        backups = await asyncio.to_thread(self.database.list_verified_backups)
        options = [item.value for item in status.recovery_options]
        if backups and "restore_backup" not in options:
            options.insert(0, "restore_backup")
        return {
            "mode": status.mode.value,
            "schema_version": status.schema_version,
            "reason_code": status.reason_code,
            "recovery_options": options,
            "backups": [{"name": item.name, "sha256": item.sha256} for item in backups],
        }

    async def restore_backup(self, name: str, sha256: str) -> int:
        return await asyncio.to_thread(self.database.restore_verified_backup, name, sha256)

    async def retry_migration(self) -> int:
        return await asyncio.to_thread(self.database.retry_initialize)

    async def close(self) -> None:
        return None


class MemoryRuntime:
    name = "memory"
    required_for_readiness = False

    def __init__(
        self,
        database: SQLiteDatabase,
        features: FeatureFlagManager,
        history: HistoryService,
        memory: MemoryService,
        memory_repository: MemoryRepository,
        cleanup_repository: PhysicalCleanupRepository,
        analyzer: MemoryCandidateAnalyzer,
        candidates: MemoryCandidateSupervisor,
        context_source: MemoryPromptContextSource,
        observer: MemoryTurnObserver,
        *,
        clock: Clock | None = None,
        maintenance_base_seconds: float = 60.0,
        maintenance_max_seconds: float = 900.0,
    ) -> None:
        if maintenance_base_seconds <= 0 or maintenance_max_seconds < maintenance_base_seconds:
            raise ValueError("maintenance backoff bounds are invalid")
        self.database = database
        self.features = features
        self.history = history
        self.memory = memory
        self.memory_repository = memory_repository
        self.cleanup_repository = cleanup_repository
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
        self._maintenance_base_seconds = maintenance_base_seconds
        self._maintenance_max_seconds = maintenance_max_seconds
        self._maintenance_failures: dict[str, int] = {}
        self._maintenance_errors: dict[str, str] = {}
        self._maintenance_next_delay = maintenance_base_seconds

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
            transition = await asyncio.to_thread(
                self.features.request_transition,
                name,
                enabled,
                updated_at=self._clock.now(),
            )
            if transition.actual_state in {
                FeatureActualState.enabled,
                FeatureActualState.disabled,
            }:
                return transition
            try:
                if name is FeatureName.long_term_memory and not enabled:
                    await self.candidates.cancel_active()
                    await asyncio.to_thread(self.memory.finalize_disabled_state)
                if name in {FeatureName.long_term_memory, FeatureName.recent_history}:
                    await self.context_source.invalidate()
                for handler in tuple(self._feature_transition_handlers):
                    await handler(transition)
            except Exception:
                return await asyncio.to_thread(
                    self.features.finish_transition,
                    name,
                    generation=transition.generation,
                    actual_state=FeatureActualState.failed,
                    reason_code="feature_transition_failed",
                    updated_at=self._clock.now(),
                )
            return await asyncio.to_thread(
                self.features.finish_transition,
                name,
                generation=transition.generation,
                actual_state=(
                    FeatureActualState.enabled if enabled else FeatureActualState.disabled
                ),
                reason_code=None,
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

    async def delete_memory(self, memory_id: str, *, user_id: str) -> DeletionResult:
        result = await asyncio.to_thread(
            self.memory.delete_logically_for_management, memory_id, user_id=user_id
        )
        if result.logical_deleted:
            await self.context_source.invalidate()
        return result

    async def confirm_memory(self, confirmation_id: str, *, approved: bool) -> MemoryItem | None:
        item = await asyncio.to_thread(self.memory.confirm, confirmation_id, approved=approved)
        if item is not None:
            await self.context_source.invalidate()
        return item

    async def pending_confirmations(self) -> tuple[PendingConfirmation, ...]:
        return await asyncio.to_thread(self.memory.pending_confirmations)

    async def clear_memories(self, *, user_id: str) -> DeletionResult:
        result = await asyncio.to_thread(
            self.memory.clear_logically_for_management, user_id=user_id
        )
        await self.context_source.invalidate()
        return result

    async def cleanup_status(self, cleanup_id: str) -> CleanupJob | None:
        return await asyncio.to_thread(self.cleanup_repository.status, cleanup_id)

    async def clear_history(self, *, user_id: str, session_id: str | None = None) -> DeletionResult:
        result = await asyncio.to_thread(
            self.history.clear_logically_for_management,
            user_id=user_id,
            session_id=session_id,
        )
        await self.context_source.invalidate()
        return result

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
                await asyncio.wait_for(self._stop.wait(), timeout=self._maintenance_next_delay)
            except TimeoutError:
                await self.run_maintenance_cycle()

    async def run_maintenance_cycle(self) -> MaintenanceSnapshot:
        """Run isolated jobs once; one failure never suppresses its siblings."""

        jobs: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("confirmations", self.memory.prune_expired_confirmations),
            ("history", self.history.cleanup),
            ("physical_cleanup", self._run_physical_cleanup),
        )
        for name, operation in jobs:
            try:
                await asyncio.to_thread(operation)
            except Exception as exc:
                self._maintenance_failures[name] = self._maintenance_failures.get(name, 0) + 1
                self._maintenance_errors[name] = _maintenance_error_code(exc)
            else:
                self._maintenance_failures.pop(name, None)
                self._maintenance_errors.pop(name, None)
        failures = max(self._maintenance_failures.values(), default=0)
        self._maintenance_next_delay = min(
            self._maintenance_base_seconds * (2 ** max(0, failures - 1)),
            self._maintenance_max_seconds,
        )
        return self.maintenance_snapshot()

    def maintenance_snapshot(self) -> MaintenanceSnapshot:
        return MaintenanceSnapshot(
            consecutive_failures=max(self._maintenance_failures.values(), default=0),
            next_delay_seconds=self._maintenance_next_delay,
            error_codes=tuple(sorted(set(self._maintenance_errors.values()))),
        )

    def _run_physical_cleanup(self) -> None:
        now = self._clock.now()
        jobs = self.cleanup_repository.list_due(now=now)
        if not jobs:
            return
        cleanup_ids = tuple(job.cleanup_id for job in jobs)
        try:
            self.database.secure_cleanup(vacuum=any(job.vacuum_required for job in jobs))
        except Exception as exc:
            code = _maintenance_error_code(exc)
            attempts = max(job.attempt_count for job in jobs) + 1
            delay = min(
                self._maintenance_base_seconds * (2 ** max(0, attempts - 1)),
                self._maintenance_max_seconds,
            )
            with suppress(Exception):
                self.cleanup_repository.mark_retry(
                    cleanup_ids,
                    now=now,
                    next_attempt_at=now + timedelta(seconds=delay),
                    reason_code=code if code in _CLEANUP_ERROR_CODES else "cleanup_failed",
                )
            raise _MaintenanceFailure(code) from exc
        self.cleanup_repository.mark_completed(cleanup_ids, now=now)

    async def check_health(self) -> CapabilityCheck:
        database_status = self.database.status
        if database_status.reason_code is not None:
            return CapabilityCheck(
                status=CapabilityState.unavailable,
                error_code=database_status.reason_code,
            )
        snapshot = self.maintenance_snapshot()
        if snapshot.error_codes:
            return CapabilityCheck(
                status=CapabilityState.degraded,
                error_code=snapshot.error_codes[0],
            )
        try:
            pending = await asyncio.to_thread(self.cleanup_repository.has_pending)
        except Exception as exc:
            code = _maintenance_error_code(exc)
            return CapabilityCheck(
                status=(
                    CapabilityState.unavailable
                    if code == "db_corrupt"
                    else CapabilityState.degraded
                ),
                error_code=code,
            )
        if pending:
            return CapabilityCheck(
                status=CapabilityState.degraded,
                error_code="cleanup_pending",
            )
        return CapabilityCheck(status=CapabilityState.ready)

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
) -> MemoryRuntime | SafeModeMemoryRuntime:
    from datetime import timedelta

    database = SQLiteDatabase(database_path, busy_timeout_ms=busy_timeout_ms)
    try:
        await asyncio.to_thread(database.initialize)
    except DatabaseSafeModeError:
        if analyzer is not None:
            await analyzer.close()
        return SafeModeMemoryRuntime(database)
    except MigrationError:
        if analyzer is not None:
            await analyzer.close()
        database.enter_safe_mode("db_migration_failed")
        return SafeModeMemoryRuntime(database)
    clock = SystemClock()
    feature_repository = FeatureFlagRepository(database)
    await asyncio.to_thread(
        feature_repository.reconcile_interrupted,
        updated_at=clock.now(),
    )
    features = await asyncio.to_thread(FeatureFlagManager, feature_repository)
    history = HistoryService(
        ConversationRepository(database),
        features,
        clock=clock,
        retention_days=retention_days,
    )
    memory_repository = MemoryRepository(database)
    cleanup_repository = PhysicalCleanupRepository(database)
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
        cleanup_repository,
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


def _maintenance_error_code(exc: Exception) -> str:
    if isinstance(exc, _MaintenanceFailure):
        return exc.code
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return "db_busy"
    if code == sqlite3.SQLITE_FULL:
        return "db_full"
    if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return "db_corrupt"
    text = str(exc).casefold()
    if "locked" in text or "busy" in text:
        return "file_locked"
    if "full" in text:
        return "db_full"
    if "corrupt" in text or "not a database" in text or "malformed" in text:
        return "db_corrupt"
    return "maintenance_failed"
