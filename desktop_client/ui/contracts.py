"""Typed, body-safe contracts for the in-process desktop bridge."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from app.schemas import (
    PipelineEvent,
    SessionReset,
    SessionSnapshotChunk,
    TurnInterruptRequest,
    UserMessage,
    utc_now,
)
from app.schemas.messages import prefixed_id

BRIDGE_PROTOCOL_VERSION: Literal[1] = 1
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class UserMessageCommand:
    """Carry the existing v1 user-message payload without a network identity."""

    payload: UserMessage
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["user.message"] = field(default="user.message", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)

    @property
    def session_id(self) -> str:
        return self.payload.session_id


@dataclass(frozen=True, slots=True)
class TurnCancelCommand:
    """Carry the existing v1 cancellation payload."""

    payload: TurnInterruptRequest
    command_id: str = field(default_factory=lambda: prefixed_id("cmd"))
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["turn.cancel"] = field(default="turn.cancel", init=False)

    def __post_init__(self) -> None:
        _validate_command_id(self.command_id)

    @property
    def session_id(self) -> str:
        return self.payload.session_id


BridgeCommand: TypeAlias = UserMessageCommand | TurnCancelCommand


class BackendState(StrEnum):
    starting = "starting"
    ready = "ready"
    restarting = "restarting"
    degraded = "degraded"
    failed = "failed"
    stopping = "stopping"
    stopped = "stopped"


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Only capabilities that the W13 shell is allowed to enable."""

    text_chat: bool = False
    turn_cancel: bool = False


@dataclass(frozen=True, slots=True)
class BackendStateEvent:
    generation: int
    state: BackendState
    reason_code: str | None = None
    capabilities: BackendCapabilities = field(default_factory=BackendCapabilities)
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["backend.state"] = field(default="backend.state", init=False)

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("backend generation must be positive")
        if self.reason_code is not None and not is_stable_reason_code(self.reason_code):
            raise ValueError("backend reason must be a stable code")


@dataclass(frozen=True, slots=True)
class CommandRejectedEvent:
    command_id: str
    reason_code: str
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["command.rejected"] = field(default="command.rejected", init=False)

    def __post_init__(self) -> None:
        if not 1 <= len(self.command_id) <= 128:
            raise ValueError("command id is outside the bridge bound")
        if not is_stable_reason_code(self.reason_code):
            raise ValueError("command rejection reason must be a stable code")


@dataclass(frozen=True, slots=True)
class BridgeOverflowEvent:
    dropped_count: int
    reason_code: Literal["event_snapshot_required"] = "event_snapshot_required"
    emitted_at: datetime = field(default_factory=utc_now)
    protocol_version: Literal[1] = field(default=BRIDGE_PROTOCOL_VERSION, init=False)
    type: Literal["bridge.overflow"] = field(default="bridge.overflow", init=False)

    def __post_init__(self) -> None:
        if self.dropped_count < 1:
            raise ValueError("overflow must report at least one dropped event")


BridgeEvent: TypeAlias = (
    PipelineEvent
    | SessionReset
    | SessionSnapshotChunk
    | BackendStateEvent
    | CommandRejectedEvent
    | BridgeOverflowEvent
)


def is_stable_reason_code(value: str) -> bool:
    return _REASON_CODE.fullmatch(value) is not None


def _validate_command_id(value: str) -> None:
    if not 1 <= len(value) <= 128:
        raise ValueError("command id is outside the bridge bound")


def is_terminal_event(event: BridgeEvent) -> bool:
    if isinstance(event, BackendStateEvent):
        return event.state in {BackendState.failed, BackendState.stopped}
    if isinstance(event, CommandRejectedEvent | BridgeOverflowEvent):
        return True
    return event.type in {
        "turn.completed",
        "turn.cancelled",
        "turn.failed",
        "assistant.completed",
        "assistant.output_incomplete",
    }
