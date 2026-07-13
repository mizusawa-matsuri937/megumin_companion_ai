"""Failure-isolated VTS authentication, reconnect, and expression queue."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from app.clients.vts.client import VTSAPIError, VTSConnectionError, VTSError
from app.clients.vts.expression_mapper import ExpressionMapper
from app.clients.vts.token_store import TokenStore, VTSToken


class VTSBridgeState(StrEnum):
    stopped = "stopped"
    connecting = "connecting"
    authorizing = "authorizing"
    ready = "ready"
    backoff = "backoff"


@dataclass(frozen=True, slots=True)
class VTSAction:
    expression: str
    turn_id: str | None = None
    action_id: str = field(default_factory=lambda: f"vts_{uuid4().hex}")


@dataclass(frozen=True, slots=True)
class VTSBridgeSnapshot:
    state: VTSBridgeState
    queue_size: int
    dropped_actions: int
    processed_actions: int
    reconnect_count: int
    missing_expression_count: int
    error_code: str | None = None


class BridgeClient(Protocol):
    async def connect(self) -> None: ...

    async def api_state(self) -> dict[str, Any]: ...

    async def request_token(self, plugin_name: str, plugin_developer: str) -> str: ...

    async def authenticate(
        self,
        plugin_name: str,
        plugin_developer: str,
        authentication_token: str,
    ) -> bool: ...

    async def trigger_hotkey(self, hotkey_id: str) -> None: ...

    async def wait_closed(self) -> None: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[], BridgeClient]
StateListener = Callable[[VTSBridgeSnapshot], None]


class VTSBridge:
    """Run VTS independently so its latency and failures never block dialogue."""

    def __init__(
        self,
        client_factory: ClientFactory,
        token_store: TokenStore,
        *,
        plugin_name: str,
        plugin_developer: str,
        expression_mapper: ExpressionMapper | None = None,
        queue_capacity: int = 16,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        state_listener: StateListener | None = None,
    ) -> None:
        if not plugin_name.strip() or not plugin_developer.strip():
            raise ValueError("VTS plugin identity 不能为空")
        if queue_capacity < 1:
            raise ValueError("queue_capacity 必须大于 0")
        if reconnect_initial_seconds < 0.0:
            raise ValueError("reconnect_initial_seconds 不能为负数")
        if reconnect_max_seconds < reconnect_initial_seconds:
            raise ValueError("reconnect_max_seconds 不能小于初始退避")
        self._client_factory = client_factory
        self._token_store = token_store
        self._plugin_name = plugin_name
        self._plugin_developer = plugin_developer
        self._mapper = expression_mapper or ExpressionMapper()
        self._queue: asyncio.Queue[VTSAction] = asyncio.Queue(maxsize=queue_capacity)
        self._reconnect_initial = reconnect_initial_seconds
        self._reconnect_max = reconnect_max_seconds
        self._state_listener = state_listener
        self._state = VTSBridgeState.stopped
        self._error_code: str | None = None
        self._dropped_actions = 0
        self._processed_actions = 0
        self._reconnect_count = 0
        self._missing_expression_count = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active_client: BridgeClient | None = None
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("VTSBridge 已关闭")
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="vts-bridge")

    def enqueue_expression(self, expression: str, *, turn_id: str | None = None) -> bool:
        """Queue the latest expression without awaiting or applying backpressure."""

        if self._closed or not expression.strip():
            return False
        action = VTSAction(expression=expression, turn_id=turn_id)
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._dropped_actions += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(action)
        except asyncio.QueueFull:
            self._dropped_actions += 1
            return False
        return True

    def snapshot(self) -> VTSBridgeSnapshot:
        return VTSBridgeSnapshot(
            state=self._state,
            queue_size=self._queue.qsize(),
            dropped_actions=self._dropped_actions,
            processed_actions=self._processed_actions,
            reconnect_count=self._reconnect_count,
            missing_expression_count=self._missing_expression_count,
            error_code=self._error_code,
        )

    async def _run(self) -> None:
        delay = self._reconnect_initial
        try:
            while not self._stop.is_set():
                try:
                    client = self._client_factory()
                except Exception:
                    self._reconnect_count += 1
                    self._set_state(VTSBridgeState.backoff, "vts_unavailable")
                    if await self._wait_for_stop(delay):
                        break
                    delay = min(
                        self._reconnect_max,
                        max(self._reconnect_initial, delay * 2),
                    )
                    continue
                self._active_client = client
                try:
                    self._set_state(VTSBridgeState.connecting)
                    await client.connect()
                    self._set_state(VTSBridgeState.authorizing)
                    api_state = await client.api_state()
                    if api_state.get("active") is not True:
                        raise VTSConnectionError
                    await self._authorize(client)
                    self._set_state(VTSBridgeState.ready)
                    delay = self._reconnect_initial
                    await self._serve_ready(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    self._reconnect_count += 1
                    error_code = exc.code if isinstance(exc, VTSError) else "vts_unavailable"
                    self._set_state(VTSBridgeState.backoff, error_code)
                    await _safe_close(client)
                    self._active_client = None
                    if await self._wait_for_stop(delay):
                        break
                    delay = min(
                        self._reconnect_max,
                        max(self._reconnect_initial, delay * 2),
                    )
                finally:
                    await _safe_close(client)
                    if self._active_client is client:
                        self._active_client = None
        finally:
            self._set_state(VTSBridgeState.stopped)

    async def _authorize(self, client: BridgeClient) -> None:
        stored = await self._token_store.load()
        if stored is not None and (
            stored.plugin_name != self._plugin_name
            or stored.plugin_developer != self._plugin_developer
        ):
            await self._token_store.delete()
            stored = None
        if stored is not None:
            authenticated = await client.authenticate(
                self._plugin_name,
                self._plugin_developer,
                stored.authentication_token,
            )
            if authenticated:
                return
            await self._token_store.delete()

        authentication_token = await client.request_token(self._plugin_name, self._plugin_developer)
        authenticated = await client.authenticate(
            self._plugin_name,
            self._plugin_developer,
            authentication_token,
        )
        if not authenticated:
            raise VTSAPIError(None)
        await self._token_store.save(
            VTSToken(
                plugin_name=self._plugin_name,
                plugin_developer=self._plugin_developer,
                authentication_token=authentication_token,
            )
        )

    async def _serve_ready(self, client: BridgeClient) -> None:
        while not self._stop.is_set():
            action_task = asyncio.create_task(self._queue.get())
            disconnected_task = asyncio.create_task(client.wait_closed())
            stopped_task = asyncio.create_task(self._stop.wait())
            tasks = {action_task, disconnected_task, stopped_task}
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stopped_task in done:
                    if action_task in done:
                        action_task.result()
                        self._queue.task_done()
                    return
                if action_task in done:
                    action = action_task.result()
                    try:
                        await self._process_action(client, action)
                    finally:
                        self._queue.task_done()
                    continue
                raise VTSConnectionError
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_action(self, client: BridgeClient, action: VTSAction) -> None:
        hotkey_id = self._mapper.hotkey_for(action.expression)
        if hotkey_id is None:
            self._missing_expression_count += 1
            self._set_state(VTSBridgeState.ready, "vts_expression_unmapped")
            return
        try:
            await client.trigger_hotkey(hotkey_id)
        except VTSAPIError:
            self._set_state(VTSBridgeState.ready, "vts_hotkey_failed")
            return
        self._processed_actions += 1
        self._set_state(VTSBridgeState.ready)

    async def _wait_for_stop(self, delay: float) -> bool:
        if delay <= 0.0:
            await asyncio.sleep(0)
            return self._stop.is_set()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    def _set_state(self, state: VTSBridgeState, error_code: str | None = None) -> None:
        self._state = state
        self._error_code = error_code
        self._notify()

    def _notify(self) -> None:
        if self._state_listener is None:
            return
        with suppress(Exception):
            self._state_listener(self.snapshot())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        active_client = self._active_client
        if active_client is not None:
            await _safe_close(active_client)
        task = self._task
        self._task = None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._set_state(VTSBridgeState.stopped)


async def _safe_close(client: BridgeClient) -> None:
    with suppress(Exception):
        await client.close()
