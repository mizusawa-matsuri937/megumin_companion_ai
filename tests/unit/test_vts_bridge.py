"""Deterministic backoff, generation, preflight, and race tests for VTS."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from app.clients.vts.bridge import VTSBridge, VTSBridgeSnapshot, VTSBridgeState
from app.clients.vts.client import (
    VTSAPIError,
    VTSConnectionError,
    VTSPreflight,
    VTSRequestTimeout,
)
from app.clients.vts.expression_mapper import ExpressionMapper
from app.clients.vts.token_store import VTSToken
from app.secret_store import VTS_TOKEN_ID, SecretStoreError, SecretStoreErrorCode


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


class _CorruptTokenStore(_MemoryTokenStore):
    async def load(self) -> VTSToken | None:
        raise SecretStoreError(SecretStoreErrorCode.corrupt, VTS_TOKEN_ID)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []
        self._permits: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        await self._permits.get()
        self.now += delay

    def advance(self) -> None:
        self._permits.put_nowait(None)


class _FakeClient:
    def __init__(
        self,
        *,
        connect_error: bool = False,
        rejected_tokens: set[str] | None = None,
        deny_token_request: bool = False,
        model_loaded: bool = True,
        available_hotkeys: set[str] | None = None,
        failing_hotkeys: set[str] | None = None,
    ) -> None:
        self.connect_error = connect_error
        self.rejected_tokens = rejected_tokens or set()
        self.deny_token_request = deny_token_request
        self.model_loaded = model_loaded
        self.available_hotkeys = available_hotkeys
        self.failing_hotkeys = failing_hotkeys or set()
        self.authenticated = True
        self.auth_tokens: list[str] = []
        self.preflight_requests: list[frozenset[str]] = []
        self.triggered: list[str] = []
        self.token_request_count = 0
        self.close_count = 0
        self._closed = asyncio.Event()

    async def connect(self) -> None:
        if self.connect_error:
            raise VTSConnectionError

    async def api_state(self) -> dict[str, object]:
        return {"active": True, "currentSessionAuthenticated": self.authenticated}

    async def request_token(self, plugin_name: str, plugin_developer: str) -> str:
        assert (plugin_name, plugin_developer) == ("Companion", "Local User")
        self.token_request_count += 1
        if self.deny_token_request:
            raise VTSAPIError(50)
        return "fresh-secret-token"

    async def authenticate(
        self,
        plugin_name: str,
        plugin_developer: str,
        authentication_token: str,
    ) -> bool:
        assert (plugin_name, plugin_developer) == ("Companion", "Local User")
        self.auth_tokens.append(authentication_token)
        self.authenticated = authentication_token not in self.rejected_tokens
        return self.authenticated

    async def preflight(self, required_hotkey_ids: frozenset[str]) -> VTSPreflight:
        self.preflight_requests.append(required_hotkey_ids)
        available = (
            required_hotkey_ids if self.available_hotkeys is None else self.available_hotkeys
        )
        missing = len(required_hotkey_ids - available)
        error = None
        if not self.authenticated:
            error = "vts_auth_revoked"
        elif not self.model_loaded:
            error = "vts_model_missing"
        elif missing:
            error = "vts_hotkey_missing"
        return VTSPreflight(
            available=True,
            authenticated=self.authenticated,
            model_loaded=self.model_loaded,
            configured_hotkey_count=len(required_hotkey_ids),
            missing_hotkey_count=missing,
            error_code=error,
        )

    async def trigger_hotkey(self, hotkey_id: str) -> None:
        if hotkey_id in self.failing_hotkeys:
            self.authenticated = False
            raise VTSAPIError(50)
        self.triggered.append(hotkey_id)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self.close_count += 1
        self._closed.set()

    def disconnect(self) -> None:
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


def _mapper(*expressions: str) -> ExpressionMapper:
    return ExpressionMapper(
        {"neutral": "neutral-key", **{item: f"{item}-key" for item in expressions}}
    )


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
        {"reconnect_initial_seconds": 0},
        {"reconnect_initial_seconds": -1},
        {"reconnect_initial_seconds": 2, "reconnect_max_seconds": 1},
    ],
)
def test_bridge_rejects_zero_and_invalid_runtime_bounds(options: dict[str, object]) -> None:
    values: dict[str, object] = {
        "client_factory": _FakeClient,
        "token_store": _MemoryTokenStore(),
        "plugin_name": "Companion",
        "plugin_developer": "Local User",
    }
    values.update(options)
    with pytest.raises(ValueError):
        VTSBridge(**values)  # type: ignore[arg-type]


def test_fake_clock_and_random_prove_backoff_jitter_cap_and_no_retry_storm() -> None:
    async def scenario() -> None:
        clock = _FakeClock()
        samples = iter([0.0, 1.0, 0.5])
        factory_calls = 0

        def unavailable() -> _FakeClient:
            nonlocal factory_calls
            factory_calls += 1
            raise VTSConnectionError

        bridge = VTSBridge(
            unavailable,
            _MemoryTokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            reconnect_initial_seconds=2,
            reconnect_max_seconds=5,
            sleep=clock.sleep,
            random_source=lambda: next(samples),
        )
        bridge.start()
        await _wait_until(lambda: len(clock.delays) == 1)
        assert clock.delays == [1.0]
        assert factory_calls == 1

        clock.advance()
        await _wait_until(lambda: len(clock.delays) == 2)
        assert clock.delays == [1.0, 4.0]
        clock.advance()
        await _wait_until(lambda: len(clock.delays) == 3)
        assert clock.delays == [1.0, 4.0, 3.75]
        assert max(clock.delays) <= 5

        for _ in range(100):
            await asyncio.sleep(0)
        assert factory_calls == 3, "one failed attempt must wait for one fake-clock advance"
        await bridge.close()

    asyncio.run(scenario())


def test_disconnect_purges_old_generation_and_reconnect_restores_neutral() -> None:
    async def scenario() -> None:
        clock = _FakeClock()
        failed = _FakeClient(connect_error=True)
        ready = _FakeClient()
        clients = iter([failed, ready])
        bridge = VTSBridge(
            lambda: next(clients),
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
            reconnect_initial_seconds=1,
            reconnect_max_seconds=2,
            sleep=clock.sleep,
            random_source=lambda: 0.0,
        )
        old_generation = bridge.begin_turn("turn-old")
        assert old_generation is not None
        assert bridge.enqueue_expression("happy", turn_id="turn-old", generation=old_generation)
        bridge.start()
        await _wait_until(lambda: len(clock.delays) == 1)
        assert bridge.snapshot().purged_actions == 1

        clock.advance()
        await _wait_until(lambda: ready.triggered == ["neutral-key"])
        assert not bridge.enqueue_expression("happy", turn_id="turn-old", generation=old_generation)
        new_generation = bridge.begin_turn("turn-new")
        assert new_generation is not None and new_generation > old_generation
        assert bridge.enqueue_expression("happy", turn_id="turn-new", generation=new_generation)
        await _wait_until(lambda: ready.triggered[-2:] == ["neutral-key", "happy-key"])
        await bridge.close()

    asyncio.run(scenario())


def test_queue_overflow_keeps_latest_current_generation_actions() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        bridge = VTSBridge(
            lambda: client,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy", "shy", "proud"),
            queue_capacity=2,
        )
        generation = bridge.begin_turn("turn-current")
        assert generation is not None
        for expression in ("happy", "shy", "proud"):
            assert bridge.enqueue_expression(
                expression, turn_id="turn-current", generation=generation
            )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().processed_actions == 2)
        assert client.triggered == ["neutral-key", "shy-key", "proud-key"]
        assert bridge.snapshot().dropped_actions == 1
        await bridge.close()

    asyncio.run(scenario())


def test_cancel_purges_pending_actions_and_recovers_neutral() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        bridge = VTSBridge(
            lambda: client,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
        )
        generation = bridge.begin_turn("turn-cancel")
        assert generation is not None
        assert bridge.enqueue_expression("happy", turn_id="turn-cancel", generation=generation)
        assert bridge.cancel_turn("turn-cancel", generation)
        assert not bridge.enqueue_expression("happy", turn_id="turn-cancel", generation=generation)
        bridge.start()
        await _wait_until(lambda: client.triggered == ["neutral-key"])
        assert bridge.snapshot().processed_actions == 0
        assert bridge.snapshot().purged_actions == 1
        await bridge.close()

    asyncio.run(scenario())


def test_simultaneous_disconnect_wins_over_queued_action() -> None:
    async def scenario() -> None:
        clock = _FakeClock()
        first = _FakeClient()
        second = _FakeClient()
        clients = iter([first, second])
        bridge = VTSBridge(
            lambda: next(clients),
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
            sleep=clock.sleep,
            random_source=lambda: 0.0,
        )
        generation = bridge.begin_turn("turn-race")
        assert generation is not None
        bridge.start()
        await _wait_until(lambda: first.triggered == ["neutral-key"])
        first.disconnect()
        assert bridge.enqueue_expression("happy", turn_id="turn-race", generation=generation)
        await _wait_until(lambda: len(clock.delays) == 1)
        assert "happy-key" not in first.triggered
        assert bridge.snapshot().purged_actions >= 1
        clock.advance()
        await _wait_until(lambda: second.triggered == ["neutral-key"])
        assert "happy-key" not in second.triggered
        await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (_FakeClient(model_loaded=False), "vts_model_missing"),
        (_FakeClient(available_hotkeys={"neutral-key"}), "vts_hotkey_missing"),
    ],
)
def test_model_and_hotkey_preflight_failures_disable_without_retry(
    client: _FakeClient, expected: str
) -> None:
    async def scenario() -> None:
        clock = _FakeClock()
        calls = 0

        def factory() -> _FakeClient:
            nonlocal calls
            calls += 1
            return client

        bridge = VTSBridge(
            factory,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
            sleep=clock.sleep,
        )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.disabled)
        assert bridge.snapshot().error_code == expected
        assert calls == 1
        assert clock.delays == []
        await bridge.close()

    asyncio.run(scenario())


def test_auth_denial_and_revocation_do_not_enter_hot_retry() -> None:
    async def scenario() -> None:
        class AuthTimeoutClient(_FakeClient):
            async def authenticate(
                self,
                plugin_name: str,
                plugin_developer: str,
                authentication_token: str,
            ) -> bool:
                del plugin_name, plugin_developer, authentication_token
                raise VTSRequestTimeout

        denied = _FakeClient(
            rejected_tokens={"expired-token"},
            deny_token_request=True,
        )
        store = _MemoryTokenStore(VTSToken("Companion", "Local User", "expired-token"))
        clock = _FakeClock()
        bridge = VTSBridge(
            lambda: denied,
            store,
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
            sleep=clock.sleep,
        )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.disabled)
        assert bridge.snapshot().error_code == "vts_auth_failed"
        assert denied.token_request_count == 1
        assert clock.delays == []
        await bridge.close()

        timed_out = AuthTimeoutClient()
        bridge = VTSBridge(
            lambda: timed_out,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
            sleep=clock.sleep,
        )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.disabled)
        assert bridge.snapshot().error_code == "vts_auth_failed"
        assert clock.delays == []
        await bridge.close()

        revoked = _FakeClient(failing_hotkeys={"happy-key"})
        store = _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token"))
        bridge = VTSBridge(
            lambda: revoked,
            store,
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
            sleep=clock.sleep,
        )
        generation = bridge.begin_turn("turn-revoked")
        assert generation is not None
        bridge.start()
        await _wait_until(lambda: revoked.triggered == ["neutral-key"])
        assert bridge.enqueue_expression("happy", turn_id="turn-revoked", generation=generation)
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.disabled)
        assert bridge.snapshot().error_code == "vts_auth_revoked"
        assert store.delete_count == 1
        assert clock.delays == []
        await bridge.close()

    asyncio.run(scenario())


def test_corrupt_token_is_content_free_and_non_retrying() -> None:
    async def scenario() -> None:
        clock = _FakeClock()
        snapshots: list[VTSBridgeSnapshot] = []
        bridge = VTSBridge(
            _FakeClient,
            _CorruptTokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
            sleep=clock.sleep,
            state_listener=snapshots.append,
        )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.disabled)
        assert bridge.snapshot().error_code == SecretStoreErrorCode.corrupt.value
        assert clock.delays == []
        assert VTS_TOKEN_ID not in repr(snapshots)
        await bridge.close()

    asyncio.run(scenario())


def test_config_edge_cases_and_non_auth_hotkey_failures_are_isolated() -> None:
    class PreflightAPIErrorClient(_FakeClient):
        async def preflight(self, required_hotkey_ids: frozenset[str]) -> VTSPreflight:
            del required_hotkey_ids
            raise VTSAPIError(999)

    class HotkeyAPIErrorClient(_FakeClient):
        async def trigger_hotkey(self, hotkey_id: str) -> None:
            if hotkey_id == "bad-key":
                raise VTSAPIError(200)
            await super().trigger_hotkey(hotkey_id)

    async def scenario() -> None:
        mismatched = _MemoryTokenStore(VTSToken("Other", "Developer", "old-token"))
        client = _FakeClient()
        bridge = VTSBridge(
            lambda: client,
            mismatched,
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
        )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.ready)
        assert mismatched.delete_count == 1
        assert mismatched.save_count == 1
        assert client.auth_tokens == ["fresh-secret-token"]
        await bridge.close()

        bridge = VTSBridge(
            _FakeClient,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=ExpressionMapper({"happy": "happy-key"}),
        )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.disabled)
        assert bridge.snapshot().error_code == "vts_hotkey_missing"
        await bridge.close()

        bridge = VTSBridge(
            PreflightAPIErrorClient,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
        )
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.disabled)
        assert bridge.snapshot().error_code == "vts_protocol_error"
        await bridge.close()

        client = HotkeyAPIErrorClient()
        bridge = VTSBridge(
            lambda: client,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=ExpressionMapper({"neutral": "neutral-key", "bad": "bad-key"}),
        )
        generation = bridge.begin_turn("turn-errors")
        assert generation is not None
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.ready)
        assert bridge.enqueue_expression("unknown", turn_id="turn-errors", generation=generation)
        assert bridge.enqueue_expression("bad", turn_id="turn-errors", generation=generation)
        await _wait_until(
            lambda: (
                bridge.snapshot().missing_expression_count == 1
                and bridge.snapshot().error_code == "vts_hotkey_failed"
            )
        )
        assert bridge.snapshot().state is VTSBridgeState.ready
        await bridge.close()

        invalid_random = VTSBridge(
            _FakeClient,
            _MemoryTokenStore(),
            plugin_name="Companion",
            plugin_developer="Local User",
            random_source=lambda: "invalid",  # type: ignore[arg-type,return-value]
        )
        assert invalid_random._retry_delay(0) == pytest.approx(0.5)
        await invalid_random.close()

    asyncio.run(scenario())


def test_close_race_joins_one_cleanup_and_rejects_new_actions() -> None:
    async def scenario() -> None:
        client = _SlowCloseClient()
        bridge = VTSBridge(
            lambda: client,
            _MemoryTokenStore(VTSToken("Companion", "Local User", "valid-token")),
            plugin_name="Companion",
            plugin_developer="Local User",
            expression_mapper=_mapper("happy"),
        )
        generation = bridge.begin_turn("turn-close")
        assert generation is not None
        bridge.start()
        await _wait_until(lambda: bridge.snapshot().state is VTSBridgeState.ready)

        first_close = asyncio.create_task(bridge.close())
        await asyncio.wait_for(client.close_started.wait(), timeout=1)
        second_close = asyncio.create_task(bridge.close())
        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close
        assert not second_close.done()
        assert not bridge.enqueue_expression("happy", turn_id="turn-close", generation=generation)

        client.finish_close.set()
        await asyncio.wait_for(second_close, timeout=1)
        await bridge.close()
        assert bridge.snapshot().state is VTSBridgeState.stopped
        with pytest.raises(RuntimeError, match="已关闭"):
            bridge.start()

    asyncio.run(scenario())
