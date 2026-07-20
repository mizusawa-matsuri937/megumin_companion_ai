"""Bounded, non-blocking communication between Qt and the backend thread."""

from __future__ import annotations

from collections import deque
from threading import Lock

from app.schemas import PipelineEvent
from PySide6.QtCore import QObject, Signal

from desktop_client.ui.contracts import (
    BridgeCommand,
    BridgeEvent,
    BridgeOverflowEvent,
    is_terminal_event,
)

DEFAULT_COMMAND_CAPACITY = 64
DEFAULT_EVENT_CAPACITY = 512
MAX_DELTA_BYTES = 64 * 1024


class ApplicationBridge(QObject):
    """Own bounded queues; Qt signals are notification-only or stable reason codes."""

    events_available = Signal()
    command_rejected = Signal(str)

    def __init__(
        self,
        *,
        command_capacity: int = DEFAULT_COMMAND_CAPACITY,
        event_capacity: int = DEFAULT_EVENT_CAPACITY,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not 1 <= command_capacity <= DEFAULT_COMMAND_CAPACITY:
            raise ValueError("command capacity must be within the W13 bound")
        if not 2 <= event_capacity <= DEFAULT_EVENT_CAPACITY:
            raise ValueError("event capacity must be within the W13 bound")
        self._command_capacity = command_capacity
        self._event_capacity = event_capacity
        self._commands: deque[BridgeCommand] = deque()
        self._events: deque[BridgeEvent] = deque()
        self._lock = Lock()
        self._event_notice_pending = False

    def submit_command(self, command: BridgeCommand) -> bool:
        """Never block the Qt thread; reject with a body-free stable code when full."""

        accepted = False
        with self._lock:
            if len(self._commands) < self._command_capacity:
                self._commands.append(command)
                accepted = True
        if not accepted:
            self.command_rejected.emit("command_queue_full")
        return accepted

    def take_commands(self, *, max_items: int = DEFAULT_COMMAND_CAPACITY) -> list[BridgeCommand]:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        with self._lock:
            count = min(max_items, len(self._commands))
            return [self._commands.popleft() for _ in range(count)]

    def publish_event(self, event: BridgeEvent) -> bool:
        """Queue an event and coalesce adjacent deltas before signalling Qt."""

        accepted = True
        should_notify = False
        with self._lock:
            if _oversized_delta(event):
                dropped_count = len(self._events) + 1
                self._events.clear()
                self._events.append(BridgeOverflowEvent(dropped_count=dropped_count))
                accepted = False
            elif self._events and isinstance(event, PipelineEvent):
                previous = self._events[-1]
                if isinstance(previous, PipelineEvent):
                    merged = _merge_delta(previous, event)
                    if merged is not None:
                        self._events[-1] = merged
                        return True
            if accepted:
                if len(self._events) >= self._event_capacity:
                    dropped_count = len(self._events)
                    self._events.clear()
                    self._events.append(BridgeOverflowEvent(dropped_count=dropped_count))
                    if is_terminal_event(event):
                        self._events.append(event)
                    else:
                        accepted = False
                else:
                    self._events.append(event)
            if not self._event_notice_pending:
                self._event_notice_pending = True
                should_notify = True
        if should_notify:
            self.events_available.emit()
        return accepted

    def drain_events(self) -> list[BridgeEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
            self._event_notice_pending = False
            return events

    def clear_sensitive(self) -> None:
        """Drop queued message bodies and events during final UI shutdown."""

        with self._lock:
            self._commands.clear()
            self._events.clear()
            self._event_notice_pending = False

    @property
    def command_count(self) -> int:
        with self._lock:
            return len(self._commands)

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)


def _merge_delta(previous: PipelineEvent, current: PipelineEvent) -> PipelineEvent | None:
    if (
        previous.type != "assistant.delta"
        or current.type != "assistant.delta"
        or previous.turn_id != current.turn_id
        or previous.session_id != current.session_id
    ):
        return None
    previous_delta = previous.payload.get("delta")
    current_delta = current.payload.get("delta")
    if not isinstance(previous_delta, str) or not isinstance(current_delta, str):
        return None
    ignored = {"delta", "coalesced_from_seq", "coalesced_through_seq", "coalesced_event_count"}
    previous_other = {key: value for key, value in previous.payload.items() if key not in ignored}
    current_other = {key: value for key, value in current.payload.items() if key != "delta"}
    if previous_other != current_other:
        return None
    combined_delta = previous_delta + current_delta
    if len(combined_delta.encode("utf-8")) > MAX_DELTA_BYTES:
        return None
    count = previous.payload.get("coalesced_event_count", 1)
    if not isinstance(count, int) or count < 1:
        count = 1
    payload = {
        **current_other,
        "delta": combined_delta,
        "coalesced_from_seq": previous.payload.get("coalesced_from_seq", previous.seq),
        "coalesced_through_seq": current.seq,
        "coalesced_event_count": count + 1,
    }
    return current.model_copy(update={"payload": payload})


def _oversized_delta(event: BridgeEvent) -> bool:
    if not isinstance(event, PipelineEvent) or event.type != "assistant.delta":
        return False
    delta = event.payload.get("delta")
    return isinstance(delta, str) and len(delta.encode("utf-8")) > MAX_DELTA_BYTES
