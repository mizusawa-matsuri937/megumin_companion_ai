"""QThread-owned asyncio backend lifecycle for the W13/W14 desktop runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from app.config import Settings
from app.core import TurnService
from app.core.idempotency import IdempotencyError
from app.core.turns import EventSubscription, SubscriptionClosedError, TurnAccessError
from app.main import create_app
from app.memory.runtime import MemoryRuntime
from app.schemas import InputMode, PipelineEvent, SessionReset, SessionSnapshotChunk
from fastapi import FastAPI
from PySide6.QtCore import QObject, QThread, QTimer, Signal

from desktop_client.inputs import PushToTalkRecorder, build_voice_input
from desktop_client.inputs.voice_input import VoiceInputError
from desktop_client.ui.bridge import ApplicationBridge
from desktop_client.ui.contracts import (
    BackendCapabilities,
    BackendState,
    BackendStateEvent,
    BridgeCommand,
    CommandRejectedEvent,
    TurnCancelCommand,
    UserMessageCommand,
    VoiceCancelCommand,
    VoiceInputState,
    VoiceStartCommand,
    VoiceStateEvent,
    VoiceStopCommand,
    VoiceUserMessageEvent,
    is_stable_reason_code,
)
from desktop_client.ui.management import DesktopManagementRuntime

DESKTOP_CLIENT_ID = "desktop_client"
FORCE_SNAPSHOT_LAST_SEQ = 9_223_372_036_854_775_807
EVENT_POLL_SECONDS = 0.02


@dataclass(frozen=True, slots=True)
class BackendContext:
    bridge: ApplicationBridge
    generation: int
    stop_event: asyncio.Event

    async def next_commands(self, *, poll_seconds: float = 0.005) -> list[BridgeCommand]:
        """Poll a bounded shared queue without creating an uncancellable executor thread."""

        while not self.stop_event.is_set():
            commands = self.bridge.take_commands()
            if commands:
                return list(commands)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue
        return []


class BackendRuntime(Protocol):
    async def run(self, context: BackendContext) -> None: ...


BackendRuntimeFactory = Callable[[int], BackendRuntime]
MAX_RETIRED_THREADS = 4


class SkeletonBackendRuntime:
    """Honest W13 runtime: lifecycle is real; chat composition remains W14 work."""

    async def run(self, context: BackendContext) -> None:
        context.bridge.publish_event(
            BackendStateEvent(
                generation=context.generation,
                state=BackendState.ready,
                capabilities=BackendCapabilities(),
            )
        )
        while not context.stop_event.is_set():
            commands = await context.next_commands()
            for command in commands:
                context.bridge.publish_event(
                    CommandRejectedEvent(
                        command_id=command.command_id,
                        reason_code="feature_not_available",
                    )
                )


class DesktopSessionCursor:
    """Keep only an event cursor across backend generations, never message bodies."""

    def __init__(self, session_id: str = "local_session") -> None:
        if not 1 <= len(session_id) <= 128:
            raise ValueError("desktop session id is outside the bridge bound")
        self.session_id = session_id
        self._lock = Lock()
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def advance(self, event: PipelineEvent) -> None:
        with self._lock:
            self._last_seq = max(self._last_seq, event.seq)

    def reset(self, event: SessionReset) -> None:
        with self._lock:
            self._last_seq = event.reset_to_seq


class TurnServiceBackendRuntime:
    """Adapt the bounded bridge to one authenticated in-process TurnService session."""

    def __init__(
        self,
        service: TurnService,
        cursor: DesktopSessionCursor,
        *,
        text_chat_available: bool,
        voice_input: PushToTalkRecorder | None = None,
        management: DesktopManagementRuntime | None = None,
        client_id: str = DESKTOP_CLIENT_ID,
    ) -> None:
        if not 1 <= len(client_id) <= 128:
            raise ValueError("desktop client id is outside the bridge bound")
        self._service = service
        self._cursor = cursor
        self._text_chat_available = text_chat_available
        self._voice_input = voice_input
        self._management = management
        self._client_id = client_id
        self._voice_stop_task: asyncio.Task[None] | None = None
        self._voice_stop_cancel_reason: str | None = None

    async def run(self, context: BackendContext) -> None:
        subscription = await self._subscribe(force_snapshot=False)
        command_task = asyncio.create_task(
            self._serve_commands(context),
            name=f"desktop-commands-{context.generation}",
        )
        capabilities = self._capabilities()
        context.bridge.publish_event(
            BackendStateEvent(
                generation=context.generation,
                state=BackendState.ready,
                capabilities=capabilities,
            )
        )
        if self._management is not None:
            await self._management.publish_initial(context.bridge, capabilities=capabilities)
        self._publish_voice_state(context, command_id=None)
        try:
            while not context.stop_event.is_set():
                if context.bridge.take_snapshot_request():
                    self._service.unsubscribe(subscription)
                    subscription = await self._subscribe(force_snapshot=True)
                    if self._management is not None:
                        await self._management.publish_initial(
                            context.bridge,
                            capabilities=capabilities,
                        )
                try:
                    item = await asyncio.wait_for(subscription.get(), timeout=EVENT_POLL_SECONDS)
                except TimeoutError:
                    continue
                except SubscriptionClosedError:
                    context.bridge.request_snapshot()
                    await asyncio.sleep(0)
                    continue
                try:
                    accepted = context.bridge.publish_event(item)
                    if accepted:
                        self._record_delivered(item)
                    else:
                        context.bridge.request_snapshot()
                finally:
                    subscription.task_done()
        finally:
            self._service.unsubscribe(subscription)
            command_task.cancel()
            await asyncio.gather(command_task, return_exceptions=True)
            voice_stop_task = self._voice_stop_task
            if voice_stop_task is not None and not voice_stop_task.done():
                voice_stop_task.cancel()
                await asyncio.gather(voice_stop_task, return_exceptions=True)
            voice_input = self._voice_input
            if voice_input is not None:
                with suppress(Exception):
                    await voice_input.cancel()

    async def _subscribe(self, *, force_snapshot: bool) -> EventSubscription:
        return await self._service.subscribe(
            self._cursor.session_id,
            client_id=self._client_id,
            last_seq=FORCE_SNAPSHOT_LAST_SEQ if force_snapshot else self._cursor.last_seq,
        )

    async def _serve_commands(self, context: BackendContext) -> None:
        while not context.stop_event.is_set():
            for command in await context.next_commands():
                await self._dispatch_command(context, command)

    async def _dispatch_command(self, context: BackendContext, command: BridgeCommand) -> None:
        management = self._management
        if management is not None and management.handles(command):
            await management.dispatch(
                context.bridge,
                command,
                capabilities=self._capabilities(),
            )
            return
        if not isinstance(
            command,
            (
                UserMessageCommand,
                TurnCancelCommand,
                VoiceStartCommand,
                VoiceStopCommand,
                VoiceCancelCommand,
            ),
        ):
            self._reject(context, command.command_id, "feature_not_available")
            return
        if command.session_id != self._cursor.session_id:
            self._reject(context, command.command_id, "identity_forbidden")
            return
        if not self._text_chat_available:
            self._reject(context, command.command_id, "feature_not_available")
            return
        try:
            if isinstance(command, VoiceStartCommand):
                await self._voice_start(context, command)
                return
            if isinstance(command, VoiceStopCommand):
                await self._voice_stop(context, command)
                return
            if isinstance(command, VoiceCancelCommand):
                await self._voice_cancel(context, command)
                return
            if isinstance(command, UserMessageCommand):
                if command.payload.input_mode is not InputMode.text:
                    self._reject(context, command.command_id, "unsupported_input_mode")
                    return
                await self._service.accept(command.payload, client_id=self._client_id)
                return
            if isinstance(command, TurnCancelCommand):
                state = await self._service.cancel(
                    client_id=self._client_id,
                    session_id=command.session_id,
                    turn_id=command.payload.turn_id,
                )
                if state is None:
                    self._reject(context, command.command_id, "turn_not_found")
                return
        except (IdempotencyError, TurnAccessError, VoiceInputError) as exc:
            reason_code = str(exc)
            self._reject(
                context,
                command.command_id,
                reason_code if is_stable_reason_code(reason_code) else "command_failed",
            )
        except Exception:
            self._reject(context, command.command_id, "command_failed")

    def _capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            text_chat=self._text_chat_available,
            turn_cancel=self._text_chat_available,
            voice_input=self._text_chat_available and self._voice_input is not None,
        )

    async def _voice_start(self, context: BackendContext, command: VoiceStartCommand) -> None:
        voice_input = self._voice_input
        if voice_input is None:
            self._reject(context, command.command_id, "feature_not_available")
            return
        try:
            await voice_input.start()
        except VoiceInputError as exc:
            self._publish_voice_state(
                context,
                command_id=command.command_id,
                failed_code=exc.code,
            )
            return
        self._publish_voice_state(context, command_id=command.command_id)

    async def _voice_stop(self, context: BackendContext, command: VoiceStopCommand) -> None:
        voice_input = self._voice_input
        if voice_input is None:
            self._reject(context, command.command_id, "feature_not_available")
            return
        current = self._voice_stop_task
        if current is not None and not current.done():
            self._publish_voice_state(
                context,
                command_id=command.command_id,
                failed_code="voice_invalid_state",
            )
            return
        context.bridge.publish_event(
            VoiceStateEvent(
                state=VoiceInputState.transcribing,
                command_id=command.command_id,
            )
        )
        task = asyncio.create_task(
            self._complete_voice_stop(context, command, voice_input),
            name=f"desktop-voice-stop-{command.command_id}",
        )
        self._voice_stop_task = task

    async def _complete_voice_stop(
        self,
        context: BackendContext,
        command: VoiceStopCommand,
        voice_input: PushToTalkRecorder,
    ) -> None:
        try:
            message = await voice_input.stop(
                session_id=self._cursor.session_id,
                user_id="local_user",
            )
            await self._service.accept(message, client_id=self._client_id)
            context.bridge.publish_event(
                VoiceUserMessageEvent(message=message, command_id=command.command_id)
            )
        except asyncio.CancelledError:
            raise
        except VoiceInputError as exc:
            if self._voice_stop_cancel_reason is not None:
                return
            self._publish_voice_state(
                context,
                command_id=command.command_id,
                failed_code=exc.code,
            )
            return
        except (IdempotencyError, TurnAccessError) as exc:
            reason_code = str(exc)
            self._reject(
                context,
                command.command_id,
                reason_code if is_stable_reason_code(reason_code) else "command_failed",
            )
            self._publish_voice_state(
                context,
                command_id=command.command_id,
                failed_code="voice_turn_failed",
            )
        except Exception:
            self._reject(context, command.command_id, "command_failed")
            self._publish_voice_state(
                context,
                command_id=command.command_id,
                failed_code="voice_turn_failed",
            )
        else:
            self._publish_voice_state(context, command_id=command.command_id)
        finally:
            if self._voice_stop_task is asyncio.current_task():
                self._voice_stop_task = None
                self._voice_stop_cancel_reason = None

    async def _voice_cancel(self, context: BackendContext, command: VoiceCancelCommand) -> None:
        voice_input = self._voice_input
        if voice_input is None:
            self._reject(context, command.command_id, "feature_not_available")
            return
        if self._voice_stop_task is not None and not self._voice_stop_task.done():
            self._voice_stop_cancel_reason = command.reason_code
        try:
            await voice_input.cancel()
        except VoiceInputError as exc:
            self._publish_voice_state(
                context,
                command_id=command.command_id,
                failed_code=exc.code,
            )
            return
        self._publish_voice_state(
            context,
            command_id=command.command_id,
            reason_code=command.reason_code,
        )

    def _publish_voice_state(
        self,
        context: BackendContext,
        *,
        command_id: str | None,
        reason_code: str | None = None,
        failed_code: str | None = None,
    ) -> None:
        if failed_code is not None:
            context.bridge.publish_event(
                VoiceStateEvent(
                    state=VoiceInputState.failed,
                    reason_code=(
                        failed_code
                        if is_stable_reason_code(failed_code)
                        else "voice_capture_failed"
                    ),
                    command_id=command_id,
                )
            )
            return
        voice_input = self._voice_input
        if voice_input is None:
            state = VoiceInputState.disabled
        else:
            try:
                state = VoiceInputState(voice_input.state.value)
            except ValueError:
                state = VoiceInputState.disabled
        context.bridge.publish_event(
            VoiceStateEvent(state=state, reason_code=reason_code, command_id=command_id)
        )

    def _reject(self, context: BackendContext, command_id: str, reason_code: str) -> None:
        context.bridge.publish_event(
            CommandRejectedEvent(command_id=command_id, reason_code=reason_code)
        )

    def _record_delivered(
        self,
        item: PipelineEvent | SessionReset | SessionSnapshotChunk,
    ) -> None:
        if isinstance(item, PipelineEvent):
            self._cursor.advance(item)
        elif isinstance(item, SessionReset):
            self._cursor.reset(item)


class DesktopChatRuntime:
    """Compose the normal application runtime without starting a network server."""

    def __init__(
        self,
        app_factory: Callable[[], FastAPI],
        cursor: DesktopSessionCursor,
        *,
        client_id: str = DESKTOP_CLIENT_ID,
    ) -> None:
        self._app_factory = app_factory
        self._cursor = cursor
        self._client_id = client_id

    async def run(self, context: BackendContext) -> None:
        try:
            application = self._app_factory()
            async with application.router.lifespan_context(application):
                service = getattr(application.state, "turn_service", None)
                settings = getattr(application.state, "settings", None)
                if not isinstance(service, TurnService) or not isinstance(settings, Settings):
                    raise RuntimeError("desktop_runtime_unavailable")
                memory_runtime = getattr(application.state, "memory_runtime", None)
                temp_registry = getattr(application.state, "temp_asset_registry", None)
                try:
                    voice_input = build_voice_input(
                        settings,
                        temp_registry=temp_registry,
                    )
                except VoiceInputError:
                    # Invalid/unsupported local STT configuration must disable
                    # only PTT; text chat stays usable and no device is opened.
                    voice_input = None
                try:
                    runtime = TurnServiceBackendRuntime(
                        service,
                        self._cursor,
                        text_chat_available=_text_chat_available(settings),
                        voice_input=voice_input,
                        management=DesktopManagementRuntime(
                            settings,
                            memory_runtime if isinstance(memory_runtime, MemoryRuntime) else None,
                        ),
                        client_id=self._client_id,
                    )
                    await runtime.run(context)
                finally:
                    if voice_input is not None:
                        with suppress(Exception):
                            await voice_input.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            context.bridge.publish_event(
                BackendStateEvent(
                    generation=context.generation,
                    state=BackendState.degraded,
                    reason_code="desktop_runtime_unavailable",
                )
            )
            await context.stop_event.wait()


class DesktopChatRuntimeFactory:
    """Preserve one body-free cursor while BackendThreadHost replaces generations."""

    def __init__(
        self,
        *,
        app_factory: Callable[[], FastAPI] = create_app,
        session_id: str = "local_session",
        client_id: str = DESKTOP_CLIENT_ID,
    ) -> None:
        self._app_factory = app_factory
        self._cursor = DesktopSessionCursor(session_id)
        self._client_id = client_id

    def __call__(self, _generation: int) -> DesktopChatRuntime:
        return DesktopChatRuntime(
            self._app_factory,
            self._cursor,
            client_id=self._client_id,
        )


def _text_chat_available(settings: Settings) -> bool:
    """Do not advertise a turn path that cannot make an idempotent claim."""

    return settings.storage.enabled and settings.llm.provider.strip().lower() != "none"


class _BackendWorker(QThread):
    def __init__(
        self,
        runtime_factory: BackendRuntimeFactory,
        bridge: ApplicationBridge,
        generation: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"BackendThread-{generation}")
        self._runtime_factory = runtime_factory
        self._bridge = bridge
        self.generation = generation
        self.crashed = False
        self._state_lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = False

    def request_stop(self) -> None:
        with self._state_lock:
            self._stop_requested = True
            loop = self._loop
            stop_event = self._stop_event
        if loop is not None and stop_event is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(stop_event.set)

    def run(self) -> None:
        self._bridge.publish_event(
            BackendStateEvent(generation=self.generation, state=BackendState.starting)
        )
        try:
            asyncio.run(self._run_runtime())
        except asyncio.CancelledError:
            with self._state_lock:
                stop_requested = self._stop_requested
            if stop_requested:
                self._publish_stopped()
            else:
                self._publish_failed()
        except BaseException:
            self._publish_failed()
        else:
            self._publish_stopped()
        finally:
            with self._state_lock:
                self._loop = None
                self._stop_event = None

    async def _run_runtime(self) -> None:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        with self._state_lock:
            self._loop = loop
            self._stop_event = stop_event
            stop_requested = self._stop_requested
        if stop_requested:
            stop_event.set()
        runtime = self._runtime_factory(self.generation)
        await runtime.run(
            BackendContext(
                bridge=self._bridge,
                generation=self.generation,
                stop_event=stop_event,
            )
        )
        if not stop_event.is_set():
            raise RuntimeError("backend runtime exited without a stop request")

    def _publish_failed(self) -> None:
        self.crashed = True
        self._bridge.publish_event(
            BackendStateEvent(
                generation=self.generation,
                state=BackendState.failed,
                reason_code="backend_crashed",
            )
        )

    def _publish_stopped(self) -> None:
        self._bridge.publish_event(
            BackendStateEvent(generation=self.generation, state=BackendState.stopped)
        )


class BackendThreadHost(QObject):
    """Own one backend generation and at most a bounded number of crash restarts."""

    generation_started = Signal(int)
    generation_finished = Signal(int, bool)
    stopped = Signal()

    def __init__(
        self,
        bridge: ApplicationBridge,
        *,
        runtime_factory: BackendRuntimeFactory | None = None,
        auto_restart_limit: int = 1,
        restart_delay_ms: int = 25,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not 0 <= auto_restart_limit <= 3:
            raise ValueError("auto restart limit must be between zero and three")
        if not 0 <= restart_delay_ms <= 5_000:
            raise ValueError("restart delay is outside the W13 bound")
        self._bridge = bridge
        self._runtime_factory = runtime_factory or DesktopChatRuntimeFactory()
        self._auto_restart_limit = auto_restart_limit
        self._restart_delay_ms = restart_delay_ms
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._start_generation)
        self._thread: _BackendWorker | None = None
        self._retired_threads: list[_BackendWorker] = []
        self._generation = 0
        self._restart_count = 0
        self._stopping = True
        self._stopped_notified = True

    def start(self) -> bool:
        if self.has_active_generation:
            return False
        self._stopping = False
        self._stopped_notified = False
        self._restart_count = 0
        self._start_generation()
        return True

    def request_stop(self) -> None:
        self._stopping = True
        self._restart_timer.stop()
        thread = self._thread
        if thread is not None:
            self._bridge.publish_event(
                BackendStateEvent(generation=thread.generation, state=BackendState.stopping)
            )
            thread.request_stop()
        else:
            self._emit_stopped_once()

    def wait_for_current_thread(self, timeout_ms: int) -> bool:
        thread = self._thread
        return thread is None or thread.wait(timeout_ms)

    @property
    def has_active_generation(self) -> bool:
        return self._thread is not None or self._restart_timer.isActive()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def retired_thread_count(self) -> int:
        return len(self._retired_threads)

    def _start_generation(self) -> None:
        if self._stopping or self._thread is not None:
            return
        self._generation += 1
        thread = _BackendWorker(
            self._runtime_factory,
            self._bridge,
            self._generation,
            self,
        )
        self._thread = thread
        thread.finished.connect(self._on_thread_finished)
        self.generation_started.emit(self._generation)
        thread.start()

    def _on_thread_finished(self) -> None:
        sender = self.sender()
        if not isinstance(sender, _BackendWorker):
            return
        thread = sender
        if self._thread is not thread:
            return
        thread.wait()
        crashed = thread.crashed
        generation = thread.generation
        self._thread = None
        thread.setParent(None)
        self._retired_threads.append(thread)
        if len(self._retired_threads) > MAX_RETIRED_THREADS:
            del self._retired_threads[0]
        self.generation_finished.emit(generation, crashed)
        if crashed and not self._stopping and self._restart_count < self._auto_restart_limit:
            self._restart_count += 1
            self._bridge.publish_event(
                BackendStateEvent(
                    generation=generation,
                    state=BackendState.restarting,
                    reason_code="backend_restart_scheduled",
                )
            )
            self._restart_timer.start(self._restart_delay_ms)
            return
        self._emit_stopped_once()

    def _emit_stopped_once(self) -> None:
        if not self._stopped_notified:
            self._stopped_notified = True
            self.stopped.emit()
