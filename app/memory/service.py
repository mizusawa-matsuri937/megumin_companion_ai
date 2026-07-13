"""Feature-aware history and long-term memory orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol

from app.emotion.clock import Clock, SystemClock
from app.memory.models import (
    ApprovedMemory,
    MemoryActionResult,
    MemoryClaim,
    MemoryDecision,
    MemoryItem,
    MemoryProposal,
    PendingConfirmation,
    ProfileItem,
    SourceInputMode,
)
from app.memory.policy import MemoryPolicy, normalize_memory_text
from app.memory.privacy import contains_credential
from app.schemas.ai import FeatureName, FeatureState
from app.storage.records import ConversationRecord


class ConfirmationNotFoundError(KeyError):
    pass


class FeatureDisabledError(RuntimeError):
    pass


class CredentialRejectedError(ValueError):
    pass


class FeatureFlags(Protocol):
    def get(self, name: FeatureName) -> FeatureState: ...

    def set(self, name: FeatureName, enabled: bool, *, updated_at: datetime) -> FeatureState: ...


class ConversationStore(Protocol):
    def add(self, record: ConversationRecord) -> bool: ...

    def list_recent(
        self,
        *,
        user_id: str,
        session_id: str,
        now: datetime,
        retention_days: int = 7,
        limit: int = 100,
        exclude_message_id: str | None = None,
    ) -> list[ConversationRecord]: ...

    def cleanup_expired(self, *, now: datetime, retention_days: int = 7) -> int: ...

    def clear(self, *, user_id: str, session_id: str | None = None) -> int: ...


class MemoryStore(Protocol):
    def upsert(self, approved: ApprovedMemory) -> MemoryItem: ...

    def search(self, *, user_id: str, query: str, limit: int = 10) -> list[MemoryItem]: ...

    def list_items(self, *, user_id: str, include_superseded: bool = False) -> list[MemoryItem]: ...

    def list_profiles(self, *, user_id: str) -> list[ProfileItem]: ...

    def update_content(
        self,
        memory_id: str,
        *,
        user_id: str,
        content: str,
        normalized_content: str,
        updated_at: datetime,
    ) -> MemoryItem | None: ...

    def delete(self, memory_id: str, *, user_id: str | None = None) -> bool: ...

    def clear(self, *, user_id: str) -> int: ...


class HistoryService:
    """Runtime history access that becomes a strict no-op when its flag is disabled."""

    def __init__(
        self,
        store: ConversationStore,
        features: FeatureFlags,
        *,
        clock: Clock | None = None,
        retention_days: int = 7,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        self._store = store
        self._features = features
        self._clock = clock or SystemClock()
        self._retention_days = retention_days
        self._lock = RLock()

    def record(self, record: ConversationRecord) -> bool:
        with self._lock:
            if not self._enabled():
                return False
            return self._store.add(record)

    def recent(
        self,
        *,
        user_id: str,
        session_id: str,
        limit: int = 100,
        exclude_message_id: str | None = None,
    ) -> list[ConversationRecord]:
        with self._lock:
            if not self._enabled():
                return []
            return self._store.list_recent(
                user_id=user_id,
                session_id=session_id,
                now=self._clock.now(),
                retention_days=self._retention_days,
                limit=limit,
                exclude_message_id=exclude_message_id,
            )

    def cleanup(self) -> int:
        with self._lock:
            if not self._enabled():
                return 0
            return self._store.cleanup_expired(
                now=self._clock.now(), retention_days=self._retention_days
            )

    def set_enabled(self, enabled: bool) -> FeatureState:
        with self._lock:
            return self._features.set(
                FeatureName.recent_history,
                enabled,
                updated_at=self._clock.now(),
            )

    def clear_for_management(self, *, user_id: str, session_id: str | None = None) -> int:
        with self._lock:
            return self._store.clear(user_id=user_id, session_id=session_id)

    def _enabled(self) -> bool:
        return self._features.get(FeatureName.recent_history).enabled


class MemoryService:
    """Gate candidate creation, confirmation and runtime retrieval behind one feature flag."""

    def __init__(
        self,
        store: MemoryStore,
        features: FeatureFlags,
        *,
        policy: MemoryPolicy | None = None,
        clock: Clock | None = None,
        confirmation_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        if confirmation_ttl.total_seconds() <= 0:
            raise ValueError("confirmation_ttl must be positive")
        self._store = store
        self._features = features
        self._policy = policy or MemoryPolicy()
        self._clock = clock or SystemClock()
        self._confirmation_ttl = confirmation_ttl
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = RLock()

    def consider_user_claim(
        self,
        claim: MemoryClaim,
        *,
        user_id: str,
        source_message_id: str,
        source_input_mode: SourceInputMode,
        source_text: str,
        created_at: datetime,
    ) -> MemoryActionResult:
        proposal = MemoryProposal(
            user_id=user_id,
            source_message_id=source_message_id,
            source_input_mode=source_input_mode,
            source_text=source_text,
            claim=claim,
            created_at=created_at,
        )
        with self._lock:
            evaluation = self._policy.evaluate(proposal)
            if not self._enabled() and evaluation.decision is not MemoryDecision.reject:
                evaluation = evaluation.model_copy(
                    update={
                        "decision": MemoryDecision.reject,
                        "reason_code": "long_term_memory_disabled",
                    }
                )
            if evaluation.decision is MemoryDecision.reject:
                return MemoryActionResult(evaluation=evaluation)
            if evaluation.decision is MemoryDecision.confirmation_required:
                pending = PendingConfirmation(
                    evaluation=evaluation,
                    expires_at=self._clock.now() + self._confirmation_ttl,
                )
                self._prune_locked()
                self._pending[pending.confirmation_id] = pending
                return MemoryActionResult(evaluation=evaluation, confirmation=pending)
            item = self._store.upsert(self._policy.approve(evaluation))
            return MemoryActionResult(evaluation=evaluation, item=item)

    def confirm(self, confirmation_id: str, *, approved: bool) -> MemoryItem | None:
        with self._lock:
            self._prune_locked()
            pending = self._pending.pop(confirmation_id, None)
            if pending is None:
                raise ConfirmationNotFoundError(confirmation_id)
            if not approved:
                return None
            if not self._enabled():
                raise FeatureDisabledError("long-term memory is disabled")
            return self._store.upsert(self._policy.approve(pending.evaluation))

    def pending_confirmations(self) -> tuple[PendingConfirmation, ...]:
        with self._lock:
            self._prune_locked()
            return tuple(sorted(self._pending.values(), key=lambda item: item.expires_at))

    def prune_expired_confirmations(self) -> int:
        """Lifecycle hook for a periodic task; other public methods also prune lazily."""

        with self._lock:
            before = len(self._pending)
            self._prune_locked()
            return before - len(self._pending)

    def retrieve_for_context(
        self, *, user_id: str, query: str, limit: int = 10
    ) -> list[MemoryItem]:
        with self._lock:
            if not self._enabled():
                return []
            return self._store.search(user_id=user_id, query=query, limit=limit)

    def profiles_for_context(self, *, user_id: str) -> list[ProfileItem]:
        with self._lock:
            if not self._enabled():
                return []
            return self._store.list_profiles(user_id=user_id)

    def list_for_management(
        self, *, user_id: str, include_superseded: bool = False
    ) -> list[MemoryItem]:
        return self._store.list_items(user_id=user_id, include_superseded=include_superseded)

    def delete_for_management(self, memory_id: str, *, user_id: str) -> bool:
        return self._store.delete(memory_id, user_id=user_id)

    def update_for_management(
        self, memory_id: str, *, user_id: str, content: str
    ) -> MemoryItem | None:
        content = " ".join(content.split())
        if not content:
            raise ValueError("memory content cannot be blank")
        if contains_credential(content):
            raise CredentialRejectedError("credential content can never be stored")
        return self._store.update_content(
            memory_id,
            user_id=user_id,
            content=content,
            normalized_content=normalize_memory_text(content),
            updated_at=self._clock.now(),
        )

    def clear_for_management(self, *, user_id: str) -> int:
        with self._lock:
            self._pending.clear()
            return self._store.clear(user_id=user_id)

    def set_enabled(self, enabled: bool) -> FeatureState:
        with self._lock:
            state = self._features.set(
                FeatureName.long_term_memory,
                enabled,
                updated_at=self._clock.now(),
            )
            if not enabled:
                self._pending.clear()
            return state

    def finalize_disabled_state(self) -> None:
        """Remove confirmations produced by workers that were already being drained."""

        with self._lock:
            if not self._enabled():
                self._pending.clear()

    def _enabled(self) -> bool:
        return self._features.get(FeatureName.long_term_memory).enabled

    def _prune_locked(self) -> None:
        now = self._clock.now()
        expired = [key for key, item in self._pending.items() if item.expires_at <= now]
        for key in expired:
            self._pending.pop(key, None)
