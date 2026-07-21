from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from app.config import Settings
from app.config.settings import LLMConfig, LoggingConfig, PipelineConfig, StorageConfig
from app.core import CancellationToken, TurnService
from app.core.idempotency import IdempotencyUnavailableError, InMemoryIdempotencyStore
from app.core.turns import TurnAccessError
from app.main import create_app
from app.paths import AppPaths
from app.schemas import (
    InputMode,
    PipelineEvent,
    SessionReset,
    SessionSnapshot,
    SessionSnapshotChunk,
    TurnInterruptRequest,
    TurnMetrics,
    TurnOutcome,
    TurnState,
    TurnStatus,
    UserMessage,
)
from desktop_client.ui.backend import (
    BackendContext,
    BackendThreadHost,
    DesktopChatRuntime,
    DesktopChatRuntimeFactory,
    DesktopSessionCursor,
    TurnServiceBackendRuntime,
    _text_chat_available,
)
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendState,
    BackendStateEvent,
    BridgeEvent,
    CommandRejectedEvent,
    SettingsSnapshotEvent,
    TurnCancelCommand,
    UserMessageCommand,
)
from desktop_client.ui.window import DesktopViewModel
from fastapi import FastAPI
from PySide6.QtWidgets import QApplication


def _pump_until(
    app: QApplication,
    predicate: Callable[[], bool],
    *,
    timeout: float = 3.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.001)
    app.processEvents()
    return predicate()


def _collect_until(
    app: QApplication,
    bridge: ApplicationBridge,
    observed: list[BridgeEvent],
    predicate: Callable[[], bool],
) -> bool:
    def collect() -> bool:
        observed.extend(bridge.drain_events())
        return predicate()

    return _pump_until(app, collect)


def _service(pipeline: _Pipeline) -> TurnService:
    return TurnService(
        logging.getLogger("test.desktop.chat.runtime"),
        pipeline,
        idempotency_store=InMemoryIdempotencyStore(),
    )


class _Pipeline:
    def __init__(self, *, hold_first: bool = False) -> None:
        self.hold_first = hold_first
        self.calls: list[str] = []
        self.first_started = asyncio.Event()

    async def run(
        self,
        message: UserMessage,
        _state: TurnState,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        self.calls.append(message.message_id)
        await emit("assistant.delta", {"delta": "reply"})
        if self.hold_first and len(self.calls) == 1:
            self.first_started.set()
            await token.wait()
            token.raise_if_cancelled()
        return TurnOutcome(
            full_text="reply",
            segments=[],
            metrics=TurnMetrics(turn_total_ms=1),
        )

    async def close(self) -> None:
        return None


class _DispatchService:
    """Small typed surface used to exercise adapter-only error mapping."""

    def __init__(self) -> None:
        self.accepted: list[str] = []
        self.cancelled: list[str | None] = []
        self.accept_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.cancel_result: TurnState | None = None

    async def accept(self, message: UserMessage, *, client_id: str) -> TurnState:
        del client_id
        if self.accept_error is not None:
            raise self.accept_error
        self.accepted.append(message.message_id)
        return TurnState(
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
        )

    async def cancel(
        self,
        *,
        client_id: str,
        session_id: str,
        turn_id: str | None,
    ) -> TurnState | None:
        del client_id, session_id
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(turn_id)
        return self.cancel_result


async def _wait_for_events(
    bridge: ApplicationBridge,
    observed: list[BridgeEvent],
    predicate: Callable[[list[BridgeEvent]], bool],
) -> None:
    for _ in range(1_000):
        observed.extend(bridge.drain_events())
        if predicate(observed):
            return
        await asyncio.sleep(0.002)
    raise AssertionError("timed out waiting for desktop bridge events")


def _runtime_host(service: TurnService) -> tuple[ApplicationBridge, BackendThreadHost]:
    bridge = ApplicationBridge()
    cursor = DesktopSessionCursor()
    host = BackendThreadHost(
        bridge,
        runtime_factory=lambda _generation: TurnServiceBackendRuntime(
            service,
            cursor,
            text_chat_available=True,
        ),
        auto_restart_limit=0,
    )
    return bridge, host


def _stop_host(app: QApplication, host: BackendThreadHost) -> None:
    host.request_stop()
    assert _pump_until(app, lambda: not host.has_active_generation)


def test_turn_service_adapter_keeps_duplicate_submission_to_one_turn(
    qapp: QApplication,
) -> None:
    pipeline = _Pipeline()
    bridge, host = _runtime_host(_service(pipeline))
    observed: list[BridgeEvent] = []
    assert host.start()
    assert _collect_until(
        qapp,
        bridge,
        observed,
        lambda: any(
            isinstance(event, BackendStateEvent) and event.state is BackendState.ready
            for event in observed
        ),
    )

    message = UserMessage(message_id="message-desktop-duplicate", text="duplicate")
    assert bridge.submit_command(UserMessageCommand(payload=message, command_id="cmd-duplicate-1"))
    assert bridge.submit_command(UserMessageCommand(payload=message, command_id="cmd-duplicate-2"))
    assert _collect_until(
        qapp,
        bridge,
        observed,
        lambda: any(
            isinstance(event, PipelineEvent) and event.type == "assistant.completed"
            for event in observed
        ),
    )

    pipeline_events = [event for event in observed if isinstance(event, PipelineEvent)]
    assert pipeline.calls == [message.message_id]
    assert sum(event.type == "turn.accepted" for event in pipeline_events) == 1
    assert sum(event.type == "turn.snapshot" for event in pipeline_events) == 1
    _stop_host(qapp, host)


def test_turn_service_adapter_new_message_uses_existing_preemption_protocol(
    qapp: QApplication,
) -> None:
    pipeline = _Pipeline(hold_first=True)
    bridge, host = _runtime_host(_service(pipeline))
    observed: list[BridgeEvent] = []
    assert host.start()
    assert _collect_until(
        qapp,
        bridge,
        observed,
        lambda: any(
            isinstance(event, BackendStateEvent) and event.state is BackendState.ready
            for event in observed
        ),
    )

    first = UserMessage(message_id="message-desktop-first", text="first")
    second = UserMessage(message_id="message-desktop-second", text="second")
    assert bridge.submit_command(UserMessageCommand(payload=first, command_id="cmd-first"))
    assert _pump_until(qapp, pipeline.first_started.is_set)
    assert bridge.submit_command(UserMessageCommand(payload=second, command_id="cmd-second"))
    assert _collect_until(
        qapp,
        bridge,
        observed,
        lambda: (
            any(
                isinstance(event, PipelineEvent)
                and event.type == "assistant.completed"
                and event.turn_id is not None
                for event in observed
            )
            and len(pipeline.calls) == 2
        ),
    )

    pipeline_events = [event for event in observed if isinstance(event, PipelineEvent)]
    first_accepted = next(event for event in pipeline_events if event.type == "turn.accepted")
    second_accepted = next(
        event
        for event in pipeline_events
        if event.type == "turn.accepted" and event.turn_id != first_accepted.turn_id
    )
    cancelled_index = next(
        index
        for index, event in enumerate(pipeline_events)
        if event.type == "turn.cancelled" and event.turn_id == first_accepted.turn_id
    )
    second_accepted_index = pipeline_events.index(second_accepted)
    assert cancelled_index < second_accepted_index
    assert pipeline.calls == [first.message_id, second.message_id]
    _stop_host(qapp, host)


def test_presenter_discards_gap_text_and_accepts_only_authoritative_snapshot() -> None:
    model = DesktopViewModel()
    state = TurnState(
        turn_id="turn-presenter",
        session_id="local_session",
        source_message_id="message-presenter",
        input_mode=InputMode.text,
        status=TurnStatus.streaming,
    )
    model.add_user_message(
        UserMessage(message_id=state.source_message_id, text="private user text")
    )
    model.apply_event(
        PipelineEvent(
            seq=1,
            type="turn.accepted",
            turn_id=state.turn_id,
            session_id=state.session_id,
            payload=state.model_dump(mode="json"),
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=2,
            type="assistant.delta",
            turn_id=state.turn_id,
            session_id=state.session_id,
            payload={"delta": "partial-private-output"},
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=4,
            type="assistant.delta",
            turn_id=state.turn_id,
            session_id=state.session_id,
            payload={"delta": "must-not-append"},
        )
    )
    assert model.snapshot_pending
    assert model.take_snapshot_request()
    assert model.transcript() == ""

    reset = SessionReset(
        reset_id="reset-presenter",
        session_id=state.session_id,
        requested_last_seq=2,
        reset_to_seq=4,
        snapshot=SessionSnapshot(
            session_id=state.session_id,
            last_seq=4,
            active_turn_ids=[state.turn_id],
            turn_count=1,
            chunk_count=1,
        ),
    )
    model.apply_event(reset)
    model.apply_event(
        SessionSnapshotChunk(
            reset_id=reset.reset_id,
            session_id=state.session_id,
            chunk_index=0,
            chunk_count=1,
            turns=[state],
        )
    )
    model.apply_event(
        PipelineEvent(
            seq=5,
            type="assistant.delta",
            turn_id=state.turn_id,
            session_id=state.session_id,
            payload={"delta": "fresh-output"},
        )
    )
    assert "partial-private-output" not in model.transcript()
    assert "must-not-append" not in model.transcript()
    assert "fresh-output" in model.transcript()


def _settings(root: Path) -> Settings:
    settings = Settings(
        logging=LoggingConfig(console_enabled=False, file_enabled=False),
        storage=StorageConfig(enabled=True, database_path=Path("companion.sqlite3")),
        llm=LLMConfig(provider="mock"),
        pipeline=PipelineConfig(mock_token_delay_ms=0, mock_audio_duration_ms=0),
    )
    settings._paths = AppPaths(root=root)
    return settings


def test_configured_desktop_runtime_restarts_with_a_body_free_snapshot(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "desktop-runtime")
    runtime_factory = DesktopChatRuntimeFactory(app_factory=lambda: create_app(settings))
    first_bridge = ApplicationBridge()
    first_host = BackendThreadHost(
        first_bridge,
        runtime_factory=runtime_factory,
        auto_restart_limit=0,
    )
    first_events: list[BridgeEvent] = []
    assert first_host.start()
    assert _collect_until(
        qapp,
        first_bridge,
        first_events,
        lambda: any(
            isinstance(event, BackendStateEvent)
            and event.state is BackendState.ready
            and event.capabilities.text_chat
            for event in first_events
        ),
    )
    assert _collect_until(
        qapp,
        first_bridge,
        first_events,
        lambda: any(isinstance(event, SettingsSnapshotEvent) for event in first_events),
    )
    message = UserMessage(message_id="message-desktop-restart", text="private restart body")
    assert first_bridge.submit_command(
        UserMessageCommand(payload=message, command_id="cmd-restart")
    )
    assert _collect_until(
        qapp,
        first_bridge,
        first_events,
        lambda: any(
            isinstance(event, PipelineEvent) and event.type == "assistant.completed"
            for event in first_events
        ),
    )
    model = DesktopViewModel()
    model.add_user_message(message)
    for event in first_events:
        model.apply_event(event)
    assert "private restart body" in model.transcript()

    initial_management_snapshots = sum(
        isinstance(event, SettingsSnapshotEvent) for event in first_events
    )
    first_bridge.request_snapshot()
    assert _collect_until(
        qapp,
        first_bridge,
        first_events,
        lambda: (
            sum(isinstance(event, SettingsSnapshotEvent) for event in first_events)
            > initial_management_snapshots
        ),
    )
    _stop_host(qapp, first_host)

    restarted_bridge = ApplicationBridge()
    restarted_host = BackendThreadHost(
        restarted_bridge,
        runtime_factory=runtime_factory,
        auto_restart_limit=0,
    )
    restarted_events: list[BridgeEvent] = []
    assert restarted_host.start()
    assert _collect_until(
        qapp,
        restarted_bridge,
        restarted_events,
        lambda: any(isinstance(event, SessionSnapshotChunk) for event in restarted_events),
    )
    for event in restarted_events:
        model.apply_event(event)

    assert any(isinstance(event, SessionReset) for event in restarted_events)
    assert any(isinstance(event, SessionSnapshotChunk) for event in restarted_events)
    assert "private restart body" not in model.transcript()
    assert not model.snapshot_pending
    _stop_host(qapp, restarted_host)


def test_adapter_direct_runtime_forwards_events_and_replaces_uncertain_stream() -> None:
    async def exercise() -> None:
        service = _service(_Pipeline())
        bridge = ApplicationBridge()
        cursor = DesktopSessionCursor()
        stop_event = asyncio.Event()
        context = BackendContext(bridge=bridge, generation=1, stop_event=stop_event)
        runtime = TurnServiceBackendRuntime(service, cursor, text_chat_available=True)
        task = asyncio.create_task(runtime.run(context))
        observed: list[BridgeEvent] = []
        try:
            await _wait_for_events(
                bridge,
                observed,
                lambda events: any(
                    isinstance(event, BackendStateEvent) and event.state is BackendState.ready
                    for event in events
                ),
            )
            message = UserMessage(message_id="message-direct-runtime", text="direct runtime")
            assert bridge.submit_command(
                UserMessageCommand(payload=message, command_id="cmd-direct-runtime")
            )
            await _wait_for_events(
                bridge,
                observed,
                lambda events: any(
                    isinstance(event, PipelineEvent) and event.type == "assistant.completed"
                    for event in events
                ),
            )
            assert cursor.last_seq > 0

            bridge.request_snapshot()
            await _wait_for_events(
                bridge,
                observed,
                lambda events: (
                    any(isinstance(event, SessionReset) for event in events)
                    and any(isinstance(event, SessionSnapshotChunk) for event in events)
                ),
            )
            reset = next(event for event in observed if isinstance(event, SessionReset))
            assert cursor.last_seq == reset.reset_to_seq
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=1)
            await service.shutdown()

    asyncio.run(exercise())


def test_adapter_dispatch_rejects_only_stable_codes() -> None:
    async def exercise() -> None:
        bridge = ApplicationBridge()
        context = BackendContext(bridge=bridge, generation=1, stop_event=asyncio.Event())
        service = _DispatchService()
        runtime = TurnServiceBackendRuntime(
            cast(TurnService, service),
            DesktopSessionCursor(),
            text_chat_available=True,
        )

        valid = UserMessage(message_id="message-dispatch-valid", text="valid")
        await runtime._dispatch_command(
            context,
            UserMessageCommand(payload=valid, command_id="cmd-dispatch-valid"),
        )
        assert service.accepted == [valid.message_id]

        await runtime._dispatch_command(
            context,
            UserMessageCommand(
                payload=UserMessage(
                    message_id="message-dispatch-wrong-session",
                    session_id="other-session",
                    text="wrong session",
                ),
                command_id="cmd-dispatch-wrong-session",
            ),
        )
        unavailable = TurnServiceBackendRuntime(
            cast(TurnService, service),
            DesktopSessionCursor(),
            text_chat_available=False,
        )
        await unavailable._dispatch_command(
            context,
            UserMessageCommand(
                payload=UserMessage(message_id="message-dispatch-unavailable", text="disabled"),
                command_id="cmd-dispatch-unavailable",
            ),
        )
        await runtime._dispatch_command(
            context,
            UserMessageCommand(
                payload=UserMessage(
                    message_id="message-dispatch-voice",
                    text="voice",
                    input_mode=InputMode.voice,
                ),
                command_id="cmd-dispatch-voice",
            ),
        )
        await runtime._dispatch_command(
            context,
            TurnCancelCommand(
                payload=TurnInterruptRequest(turn_id="turn-missing"),
                command_id="cmd-dispatch-missing",
            ),
        )
        service.accept_error = IdempotencyUnavailableError()
        await runtime._dispatch_command(
            context,
            UserMessageCommand(
                payload=UserMessage(message_id="message-dispatch-idempotency", text="retry"),
                command_id="cmd-dispatch-idempotency",
            ),
        )
        service.accept_error = RuntimeError("PRIVATE_ADAPTER_EXCEPTION_BODY")
        await runtime._dispatch_command(
            context,
            UserMessageCommand(
                payload=UserMessage(message_id="message-dispatch-unknown", text="unknown"),
                command_id="cmd-dispatch-unknown",
            ),
        )
        service.accept_error = None
        service.cancel_error = TurnAccessError()
        await runtime._dispatch_command(
            context,
            TurnCancelCommand(
                payload=TurnInterruptRequest(turn_id="turn-forbidden"),
                command_id="cmd-dispatch-forbidden",
            ),
        )

        events = bridge.drain_events()
        rejections = [event for event in events if isinstance(event, CommandRejectedEvent)]
        assert {event.reason_code for event in rejections} == {
            "identity_forbidden",
            "feature_not_available",
            "unsupported_input_mode",
            "turn_not_found",
            "idempotency_unavailable",
            "command_failed",
            "turn_forbidden",
        }
        assert "PRIVATE_ADAPTER_EXCEPTION_BODY" not in repr(rejections)

    asyncio.run(exercise())


def test_desktop_runtime_degrades_without_body_and_cursor_is_metadata_only() -> None:
    async def exercise() -> None:
        bridge = ApplicationBridge()
        stop_event = asyncio.Event()
        runtime = DesktopChatRuntime(FastAPI, DesktopSessionCursor())
        task = asyncio.create_task(
            runtime.run(BackendContext(bridge=bridge, generation=1, stop_event=stop_event))
        )
        observed: list[BridgeEvent] = []
        try:
            await _wait_for_events(
                bridge,
                observed,
                lambda events: any(
                    isinstance(event, BackendStateEvent)
                    and event.state is BackendState.degraded
                    and event.reason_code == "desktop_runtime_unavailable"
                    for event in events
                ),
            )
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())

    with pytest.raises(ValueError):
        DesktopSessionCursor("")
    factory = DesktopChatRuntimeFactory(app_factory=FastAPI, session_id="metadata-only")
    assert isinstance(factory(1), DesktopChatRuntime)
    enabled = _settings(Path("desktop-runtime-settings"))
    assert _text_chat_available(enabled)
    disabled = enabled.model_copy(update={"storage": StorageConfig(enabled=False)})
    assert not _text_chat_available(disabled)
