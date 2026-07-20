from __future__ import annotations

from app.schemas import PipelineEvent, TurnInterruptRequest, UserMessage
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendState,
    BackendStateEvent,
    BridgeOverflowEvent,
    CommandRejectedEvent,
    TurnCancelCommand,
    UserMessageCommand,
)
from PySide6.QtWidgets import QApplication


def _delta(seq: int, text: str = "x", *, turn_id: str = "turn_1") -> PipelineEvent:
    return PipelineEvent(
        seq=seq,
        type="assistant.delta",
        turn_id=turn_id,
        session_id="local_session",
        payload={"delta": text},
    )


def test_command_queue_is_bounded_and_non_blocking(qapp: QApplication) -> None:
    bridge = ApplicationBridge(command_capacity=2)
    rejected: list[str] = []
    bridge.command_rejected.connect(rejected.append)
    first = UserMessageCommand(payload=UserMessage(text="one"))
    second = UserMessageCommand(payload=UserMessage(text="two"))
    third = UserMessageCommand(payload=UserMessage(text="private sentinel"))

    assert bridge.submit_command(first)
    assert bridge.submit_command(second)
    assert not bridge.submit_command(third)
    assert rejected == ["command_queue_full"]
    assert bridge.command_count == 2
    assert bridge.take_commands(max_items=1) == [first]
    assert bridge.take_commands() == [second]
    assert bridge.command_count == 0
    qapp.processEvents()


def test_ten_thousand_deltas_coalesce_to_one_bounded_event(qapp: QApplication) -> None:
    bridge = ApplicationBridge()
    notices: list[bool] = []
    bridge.events_available.connect(lambda: notices.append(True))

    for seq in range(1, 10_001):
        assert bridge.publish_event(_delta(seq))

    assert bridge.event_count == 1
    events = bridge.drain_events()
    assert notices == [True]
    assert len(events) == 1
    merged = events[0]
    assert isinstance(merged, PipelineEvent)
    assert merged.seq == 10_000
    assert merged.payload["delta"] == "x" * 10_000
    assert merged.payload["coalesced_from_seq"] == 1
    assert merged.payload["coalesced_through_seq"] == 10_000
    assert merged.payload["coalesced_event_count"] == 10_000
    qapp.processEvents()


def test_event_overflow_requires_snapshot_and_preserves_terminal_event(
    qapp: QApplication,
) -> None:
    bridge = ApplicationBridge(event_capacity=2)
    assert bridge.publish_event(_delta(1, turn_id="turn_1"))
    assert bridge.publish_event(_delta(2, turn_id="turn_2"))
    terminal = PipelineEvent(
        seq=3,
        type="turn.failed",
        turn_id="turn_3",
        session_id="local_session",
        payload={"error_code": "synthetic_failure"},
    )

    assert bridge.publish_event(terminal)
    events = bridge.drain_events()
    assert len(events) == 2
    assert isinstance(events[0], BridgeOverflowEvent)
    assert events[0].dropped_count == 2
    assert events[1] is terminal

    assert bridge.publish_event(_delta(4, turn_id="turn_4"))
    assert bridge.publish_event(_delta(5, turn_id="turn_5"))
    assert not bridge.publish_event(_delta(6, turn_id="turn_6"))
    overflow_only = bridge.drain_events()
    assert len(overflow_only) == 1
    assert isinstance(overflow_only[0], BridgeOverflowEvent)

    assert not bridge.publish_event(_delta(7, "界" * 30_000, turn_id="turn_large"))
    oversized = bridge.drain_events()
    assert len(oversized) == 1
    assert isinstance(oversized[0], BridgeOverflowEvent)
    qapp.processEvents()


def test_bridge_validates_bounds_and_clears_sensitive_queues(qapp: QApplication) -> None:
    for command_capacity, event_capacity in ((0, 2), (65, 2), (1, 1), (1, 513)):
        try:
            ApplicationBridge(
                command_capacity=command_capacity,
                event_capacity=event_capacity,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid bridge bounds were accepted")

    bridge = ApplicationBridge()
    bridge.submit_command(UserMessageCommand(payload=UserMessage(text="draft sentinel")))
    bridge.publish_event(BackendStateEvent(generation=1, state=BackendState.ready))
    bridge.clear_sensitive()
    assert bridge.command_count == 0
    assert bridge.event_count == 0
    try:
        bridge.take_commands(max_items=0)
    except ValueError:
        pass
    else:
        raise AssertionError("zero command batch was accepted")
    for factory in (
        lambda: BackendStateEvent(generation=0, state=BackendState.ready),
        lambda: BackendStateEvent(
            generation=1,
            state=BackendState.failed,
            reason_code="PRIVATE exception body",
        ),
        lambda: CommandRejectedEvent(command_id="", reason_code="busy"),
        lambda: CommandRejectedEvent(command_id="cmd_1", reason_code="not stable"),
        lambda: BridgeOverflowEvent(dropped_count=0),
        lambda: UserMessageCommand(payload=UserMessage(text="valid"), command_id=""),
        lambda: TurnCancelCommand(
            payload=TurnInterruptRequest(),
            command_id="x" * 129,
        ),
    ):
        try:
            factory()
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe bridge event was accepted")
    qapp.processEvents()
