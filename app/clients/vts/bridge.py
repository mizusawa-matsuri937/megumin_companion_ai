"""Failure-isolated VTS preflight, reconnect, generation, and expression queue."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from app.clients.vts.client import (
    VTSAPIError,
    VTSAuthenticationError,
    VTSConfigurationError,
    VTSConnectionError,
    VTSError,
    VTSPreflight,
    VTSProtocolError,
    VTSRequestTimeout,
)
from app.clients.vts.expression_mapper import ExpressionMapper
from app.clients.vts.token_store import TokenStore, VTSToken
from app.secret_store import SecretStoreError


class VTSBridgeState(StrEnum):
    stopped = "stopped"
    connecting = "connecting"
    authorizing = "authorizing"
    preflighting = "preflighting"
    ready = "ready"
    backoff = "backoff"
    disabled = "disabled"


@dataclass(frozen=True, slots=True)
class VTSAction:
    expression: str
    generation: int
    turn_id: str
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
    generation: int = 0
    purged_actions: int = 0
    stale_actions: int = 0
    neutral_resets: int = 0
    preflight_ready: bool = False
    configured_hotkey_count: int = 0
    missing_hotkey_count: int = 0


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

    async def preflight(self, required_hotkey_ids: frozenset[str]) -> VTSPreflight: ...

    async def trigger_hotkey(self, hotkey_id: str) -> None: ...

    async def wait_closed(self) -> None: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[], BridgeClient]
StateListener = Callable[[VTSBridgeSnapshot], None]
Sleep = Callable[[float], Awaitable[None]]
RandomSource = Callable[[], float]


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
        sleep: Sleep = asyncio.sleep,
        random_source: RandomSource = random.random,
    ) -> None:
        if not plugin_name.strip() or not plugin_developer.strip():
            raise ValueError("VTS plugin identity 不能为空")
        if queue_capacity < 1:
            raise ValueError("queue_capacity 必须大于 0")
        if reconnect_initial_seconds <= 0.0:
            raise ValueError("reconnect_initial_seconds 必须大于 0")
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
        self._sleep = sleep
        self._random_source = random_source
        self._state_listener = state_listener
        self._state = VTSBridgeState.stopped
        self._error_code: str | None = None
        self._dropped_actions = 0
        self._processed_actions = 0
        self._reconnect_count = 0
        self._missing_expression_count = 0
        self._purged_actions = 0
        self._stale_actions = 0
        self._neutral_resets = 0
        self._generation = 0
        self._current_turn_id: str | None = None
        self._neutral_pending = True
        self._preflight = VTSPreflight(False, False, False, 0, 0)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active_client: BridgeClient | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("VTSBridge 已关闭")
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="vts-bridge")

    def begin_turn(self, turn_id: str) -> int | None:
        """Advance the trusted generation before accepting actions for a new turn."""

        if self._closed or self._state is VTSBridgeState.disabled or not turn_id.strip():
            return None
        self._advance_generation(turn_id)
        return self._generation

    def cancel_turn(self, turn_id: str, generation: int) -> bool:
        """Retire one active generation and schedule a neutral recovery."""

        if self._closed or generation != self._generation or turn_id != self._current_turn_id:
            return False
        self._advance_generation(None)
        return True

    def enqueue_expression(self, expression: str, *, turn_id: str, generation: int) -> bool:
        """Queue metadata only when both turn identity and generation are current."""

        if (
            self._closed
            or self._state is VTSBridgeState.disabled
            or not expression.strip()
            or not turn_id.strip()
        ):
            return False
        if generation != self._generation or turn_id != self._current_turn_id:
            self._stale_actions += 1
            return False
        action = VTSAction(expression=expression, turn_id=turn_id, generation=generation)
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
            generation=self._generation,
            purged_actions=self._purged_actions,
            stale_actions=self._stale_actions,
            neutral_resets=self._neutral_resets,
            preflight_ready=self._preflight.ready,
            configured_hotkey_count=self._preflight.configured_hotkey_count,
            missing_hotkey_count=self._preflight.missing_hotkey_count,
        )

    async def _run(self) -> None:
        retry_attempt = 0
        try:
            while not self._stop.is_set():
                client: BridgeClient | None = None
                try:
                    client = self._client_factory()
                    self._active_client = client
                    self._set_state(VTSBridgeState.connecting)
                    await client.connect()
                    self._set_state(VTSBridgeState.authorizing)
                    api_state = await client.api_state()
                    if api_state.get("active") is not True:
                        raise VTSConfigurationError("vts_api_unavailable")
                    await self._authorize(client)
                    self._set_state(VTSBridgeState.preflighting)
                    await self._perform_preflight(client)
                    self._set_state(VTSBridgeState.ready)
                    retry_attempt = 0
                    await self._serve_ready(client)
                except asyncio.CancelledError:
                    raise
                except (
                    VTSAuthenticationError,
                    VTSAPIError,
                    VTSConfigurationError,
                    VTSProtocolError,
                    SecretStoreError,
                    ValueError,
                ) as exc:
                    if self._stop.is_set():
                        break
                    await self._disable(exc)
                    break
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    self._reconnect_count += 1
                    self._preflight = VTSPreflight(False, False, False, 0, 0)
                    self._advance_generation(None)
                    error_code = exc.code if isinstance(exc, VTSError) else "vts_unavailable"
                    self._set_state(VTSBridgeState.backoff, error_code)
                    delay = self._retry_delay(retry_attempt)
                    retry_attempt += 1
                    if client is not None:
                        await _safe_close(client)
                    self._active_client = None
                    if await self._wait_for_stop(delay):
                        break
                finally:
                    if client is not None:
                        await _safe_close(client)
                    if self._active_client is client:
                        self._active_client = None
        finally:
            if self._stop.is_set():
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
            try:
                authenticated = await client.authenticate(
                    self._plugin_name,
                    self._plugin_developer,
                    stored.authentication_token,
                )
            except VTSRequestTimeout as exc:
                raise VTSAuthenticationError from exc
            if authenticated:
                return
            await self._token_store.delete()

        try:
            authentication_token = await client.request_token(
                self._plugin_name, self._plugin_developer
            )
            authenticated = await client.authenticate(
                self._plugin_name,
                self._plugin_developer,
                authentication_token,
            )
        except (VTSAPIError, VTSRequestTimeout) as exc:
            raise VTSAuthenticationError from exc
        if not authenticated:
            raise VTSAuthenticationError
        await self._token_store.save(
            VTSToken(
                plugin_name=self._plugin_name,
                plugin_developer=self._plugin_developer,
                authentication_token=authentication_token,
            )
        )

    async def _perform_preflight(self, client: BridgeClient) -> None:
        if self._mapper.hotkey_for("neutral") is None:
            raise VTSConfigurationError("vts_hotkey_missing")
        try:
            result = await client.preflight(self._mapper.required_hotkey_ids())
        except VTSAPIError as exc:
            raise VTSProtocolError from exc
        self._preflight = result
        if not result.ready:
            assert result.error_code is not None
            raise VTSConfigurationError(result.error_code)

    async def _serve_ready(self, client: BridgeClient) -> None:
        while not self._stop.is_set():
            if self._neutral_pending:
                await self._restore_neutral(client)
                continue
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
                        self._purged_actions += 1
                    return
                if disconnected_task in done:
                    if action_task in done:
                        action_task.result()
                        self._queue.task_done()
                        self._purged_actions += 1
                    raise VTSConnectionError
                action = action_task.result()
                try:
                    await self._process_action(client, action)
                finally:
                    self._queue.task_done()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _restore_neutral(self, client: BridgeClient) -> None:
        hotkey_id = self._mapper.hotkey_for("neutral")
        if hotkey_id is None:
            raise VTSConfigurationError("vts_hotkey_missing")
        try:
            await client.trigger_hotkey(hotkey_id)
        except VTSAPIError:
            await self._raise_if_auth_revoked(client)
            raise VTSConfigurationError("vts_hotkey_missing") from None
        self._neutral_pending = False
        self._neutral_resets += 1
        self._set_state(VTSBridgeState.ready)

    async def _process_action(self, client: BridgeClient, action: VTSAction) -> None:
        if action.generation != self._generation or action.turn_id != self._current_turn_id:
            self._stale_actions += 1
            return
        hotkey_id = self._mapper.hotkey_for(action.expression)
        if hotkey_id is None:
            self._missing_expression_count += 1
            self._set_state(VTSBridgeState.ready, "vts_expression_unmapped")
            return
        try:
            await client.trigger_hotkey(hotkey_id)
        except VTSAPIError:
            await self._raise_if_auth_revoked(client)
            self._set_state(VTSBridgeState.ready, "vts_hotkey_failed")
            return
        self._processed_actions += 1
        self._set_state(VTSBridgeState.ready)

    async def _raise_if_auth_revoked(self, client: BridgeClient) -> None:
        state = await client.api_state()
        authenticated = state.get("currentSessionAuthenticated")
        if not isinstance(authenticated, bool):
            raise VTSProtocolError
        if not authenticated:
            raise VTSConfigurationError("vts_auth_revoked")

    async def _disable(self, exc: Exception) -> None:
        if isinstance(exc, SecretStoreError):
            error_code = exc.code.value
        elif isinstance(exc, VTSAPIError):
            error_code = "vts_config"
        elif isinstance(exc, VTSError):
            error_code = exc.code
        else:
            error_code = "vts_config"
        if error_code == "vts_auth_revoked":
            with suppress(Exception):
                await self._token_store.delete()
        if self._preflight.error_code != error_code:
            self._preflight = VTSPreflight(False, False, False, 0, 0, error_code)
        self._advance_generation(None)
        self._set_state(VTSBridgeState.disabled, error_code)
        active_client = self._active_client
        if active_client is not None:
            await _safe_close(active_client)
            if self._active_client is active_client:
                self._active_client = None
        await self._stop.wait()

    def _retry_delay(self, attempt: int) -> float:
        exponent = min(max(attempt, 0), 30)
        base = min(self._reconnect_max, self._reconnect_initial * (2**exponent))
        try:
            sample: float = float(self._random_source())
        except (TypeError, ValueError):
            sample = 0.0
        sample = min(1.0, max(0.0, sample))
        jittered: float = float(base) * (0.5 + 0.5 * sample)
        return float(min(self._reconnect_max, jittered))

    async def _wait_for_stop(self, delay: float) -> bool:
        sleeper: asyncio.Future[None] = asyncio.ensure_future(self._sleep(delay))
        stopped = asyncio.create_task(self._stop.wait())
        tasks: set[asyncio.Future[Any]] = {sleeper, stopped}
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stopped in done:
                return True
            sleeper.result()
            return self._stop.is_set()
        finally:
            for task in (sleeper, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, stopped, return_exceptions=True)

    def _advance_generation(self, turn_id: str | None) -> None:
        self._generation += 1
        self._current_turn_id = turn_id
        self._purge_queue()
        self._neutral_pending = True
        self._notify()

    def _purge_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._queue.task_done()
            self._purged_actions += 1

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
        task = self._close_task
        if task is None:
            self._closed = True
            self._stop.set()
            self._generation += 1
            self._current_turn_id = None
            self._neutral_pending = False
            self._purge_queue()
            task = asyncio.create_task(self._close(), name="vts-bridge-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close(self) -> None:
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
