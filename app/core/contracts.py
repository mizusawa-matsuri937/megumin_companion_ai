"""Small runtime protocols that keep optional AI modules dependency-injected."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.schemas import FeatureName, FeatureState, PipelineEvent, TurnState, UserMessage

FeatureListener = Callable[[FeatureState], None]
Unsubscribe = Callable[[], None]


class TurnEventSink(Protocol):
    """Accept events synchronously; implementations must never perform network I/O here."""

    def publish(self, event: PipelineEvent) -> bool:
        """Queue an event without waiting, returning False when it was dropped."""

        ...

    async def close(self) -> None: ...


class FeatureFlagSource(Protocol):
    """Expose an in-memory feature snapshot with explicit change subscriptions."""

    def get_feature(self, name: FeatureName) -> FeatureState: ...

    def subscribe(self, listener: FeatureListener) -> Unsubscribe: ...


class UserMessageSink(Protocol):
    """Common destination for explicit text and local STT input."""

    async def accept(self, message: UserMessage) -> TurnState: ...


class TurnPriorityController(Protocol):
    """Atomically arbitrate explicit user turns against optional background work."""

    async def begin_user_turn(self, turn_id: str) -> None: ...

    async def end_user_turn(self, turn_id: str) -> None: ...
