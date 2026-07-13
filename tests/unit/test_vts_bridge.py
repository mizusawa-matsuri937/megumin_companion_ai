"""Deterministic tests for the bounded, reconnecting VTS bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from app.clients.vts.bridge import VTSBridge, VTSBridgeSnapshot, VTSBridgeState
from app.clients.vts.client import VTSAPIError, VTSConnectionError
from app.clients.vts.expression_mapper import ExpressionMapper
from app.clients.vts.token_store import VTSToken


@dataclass
class _MemoryTokenStore:
    token: VTSToken | None = None
    delete_count: int = 0
    save_count: int = 0

    async def load(self) -> VTSToken | None:
        return self.token

    async def save(self, token: VTSToken) -> None:
        self.token = token
        self.save_count += 1

    async def delete(self) -> None:
        self.token = None
        self.delete_count += 1


class _FakeClient:
    def __init__(
        self,
        *,
        connect_error: bool = False,
        rejected_tokens: set[str] | None = None,
        failing_hotkeys: set[str] | None = None,
    ) -> None:
        self.connect_error = connect_error
        self.rejected_tokens = rejected_tokens or set()
        self.failing_hotkeys = failing_hotkeys or set()
        self.auth_tokens: list[str] = []
        self.triggered: list[str] = []
        self.token_request_count = 0
        self.close_count = 0
        self._closed = asyncio.Event()

    async def connect(self) -> None:
        if self.connect_error:
            raise VTSConnectionError

    async def api_state(self) -> dict[str, object]:
        return {"active": True}

    async def request_token(self, plugin_name: str, plugin_developer: str) -> str:
        assert (plugin_name, plugin_developer) == ("Companion", "Local User")
        self.token_request_count += 1
        return "fresh-secret-token"

    async def authenticate(
        self,
        plugin_name: str,
        plugin_developer: str,
        authentication_token: str,
    ) -> bool:
        assert (plugin_name, plugin_developer) == ("Companion", "Local User")
        self.auth_tokens.append(authentication_token)
        return authentication_token not in self.rejected_tokens

    async def trigger_hotkey(self, hotkey_id: str) -> None:
        if hotkey_id in self.failing_hotkeys:
            raise VTSAPIError(200)
        self.triggered.append(hotkey_id)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self.close_count += 1
        self._closed.set()


class _SlowCloseClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.finish_close = asyncio.Event()

    async def close(self) -> None:
        self.close_count += 1
        self.close_started.set()
        await self.finish_close.wait()
        self._closed.set()


async def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


@pytest.mark.parametrize(
    "options",
    [
        {"plugin_name": ""},
        {"queue_capacity": 0},
        {"reconnect_initial_seconds": -1},
        {"reconnect_initial_seconds": 2, "reconnect_max_seconds": 1},
    ],
)
def test_bridge_rejects_invalid_runtime_bounds(options: dict[str, object]) -> None:
    values: dict[str, object] = {
        "client_factory": _FakeClient,
        "token_store": _MemoryTokenStore(),
        "plugin_name": "Companion",
        "plugin_developer": "Local User",
    }
    values.update(options)
    with pytest.raises(ValueError):
        VTSBridge(**values)  # type: ignore[arg-type]


def test_closed_bridge_is_idempotent_and_rejects_new_actions() -> None:
    async def scenario() -> None:
        bridge = VTSBridge(
            _FakeClient,
            _MemoryTokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
        )
        assert not bridge.enqueue_expression("")
        await bridge.close()
        await bridge.close()
        assert not bridge.enqueue_expression("happy")
        with pytest.raises(RuntimeError, match="已关闭"):
            bridge.start()

    asyncio.run(scenario())


def test_concurrent_close_waiters_join_the_same_bridge_cleanup() -> None:
    async def scenario() -> None:
        client = _SlowCloseClient()
        bridge = VTSBridge(
            lambda: client,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            reconnect_initial_seconds=0,
            reconnect_max_seconds=0,
        )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.ready)

        first_close = asyncio.create_task(bridge.close())
        await asyncio.wait_for(client.close_started.wait(), timeout=1)
        second_close = asyncio.create_task(bridge.close())
        await asyncio.sleep(0)
        assert not first_close.done()
        assert not second_close.done()
        assert not bridge.enqueue_expression("happy")

        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close
        assert not second_close.done()

        client.finish_close.set()
        await asyncio.wait_for(second_close, timeout=1)
        await bridge.close()

        assert client._closed.is_set()
        assert bridge.snapshot().state is VTSBridgeState.stopped
        with pytest.raises(RuntimeError, match="已关闭"):
            bridge.start()

    asyncio.run(scenario())


def test_bridge_replaces_rejected_token_and_keeps_latest_bounded_actions() -> None:
    async def scenario() -> None:
        expired = VTSToken("Companion", "Local User", "expired-secret-token")
        store = _MemoryTokenStore(expired)
        client = _FakeClient(rejected_tokens={"expired-secret-token"})
        snapshots: list[VTSBridgeSnapshot] = []
        bridge = VTSBridge(
            lambda: client,
            store,
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=ExpressionMapper(
                {"happy": "happy-key", "shy": "shy-key", "proud": "proud-key"}
            ),
            queue_capacity=2,
            reconnect_initial_seconds=0,
            reconnect_max_seconds=0,
            state_listener=snapshots.append,
        )

        assert bridge.enqueue_expression("happy", turn_id="turn_old")
        assert bridge.enqueue_expression("shy", turn_id="turn_new")
        assert bridge.enqueue_expression("proud", turn_id="turn_new")
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().processed_actions == 2)

        snapshot = bridge.snapshot()
        assert snapshot.state is VTSBridgeState.ready
        assert snapshot.dropped_actions == 1
        assert snapshot.queue_size == 0
        assert client.auth_tokens == ["expired-secret-token", "fresh-secret-token"]
        assert client.token_request_count == 1
        assert client.triggered == ["shy-key", "proud-key"]
        assert store.delete_count == 1
        assert store.save_count == 1
        assert store.token is not None
        assert "fresh-secret-token" not in repr(snapshots)

        await bridge.close()
        assert bridge.snapshot().state is VTSBridgeState.stopped

    asyncio.run(scenario())


def test_bridge_reconnects_after_connection_failure_without_losing_dialogue_action() -> None:
    async def scenario() -> None:
        failed = _FakeClient(connect_error=True)
        ready = _FakeClient()
        clients = iter([failed, ready])
        store = _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token"))
        states: list[VTSBridgeState] = []
        bridge = VTSBridge(
            lambda: next(clients),
            store,
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=ExpressionMapper({"happy": "happy-key"}),
            reconnect_initial_seconds=0,
            reconnect_max_seconds=0,
            state_listener=lambda snapshot: states.append(snapshot.state),
        )

        assert bridge.enqueue_expression("happy")
        bridge.start()
        await _wait_until(lambda: ready.triggered == ["happy-key"])

        assert bridge.snapshot().reconnect_count == 1
        assert VTSBridgeState.backoff in states
        assert bridge.snapshot().state is VTSBridgeState.ready
        await bridge.close()

    asyncio.run(scenario())


def test_unmapped_and_missing_hotkeys_are_isolated_and_next_action_still_runs() -> None:
    async def scenario() -> None:
        client = _FakeClient(failing_hotkeys={"missing-key"})
        store = _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token"))
        bridge = VTSBridge(
            lambda: client,
            store,
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=ExpressionMapper({"bad": "missing-key", "neutral": "neutral-key"}),
            reconnect_initial_seconds=0,
            reconnect_max_seconds=0,
        )

        bridge.start()
        assert bridge.enqueue_expression("unknown")
        assert bridge.enqueue_expression("bad")
        await _wait_until(
            lambda: (
                bridge.snapshot().missing_expression_count == 1
                and bridge.snapshot().queue_size == 0
                and bridge.snapshot().error_code == "vts_hotkey_failed"
            )
        )
        assert bridge.snapshot().state is VTSBridgeState.ready
        assert bridge.snapshot().error_code == "vts_hotkey_failed"

        assert bridge.enqueue_expression("neutral")
        await _wait_until(lambda: client.triggered == ["neutral-key"])
        assert bridge.snapshot().processed_actions == 1
        assert bridge.snapshot().error_code is None
        await bridge.close()

    asyncio.run(scenario())
