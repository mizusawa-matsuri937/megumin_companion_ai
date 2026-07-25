"""Explicit W19 GPT-SoVITS/VTube Studio setup preflight.

The runner is owned by BackendThread.  It emits only bounded capability
states: provider response bodies, reference paths, prompt text, model names,
hotkey identifiers, tokens, and generated WAV paths never cross the UI bridge.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Protocol

from app.clients.tts import GPTSoVITSPreset, GPTSoVITSProbe, GPTSoVITSProvider
from app.clients.vts import (
    DPAPITokenStore,
    ExpressionMapper,
    VTSBridge,
    VTSBridgeSnapshot,
    VTSBridgeState,
    VTSClient,
)
from app.config import Settings
from app.core.cancellation import CancellationToken
from app.schemas import AudioResult, TTSJob
from app.secret_store import vts_token_file
from app.temp_assets import TempAssetRegistry

from desktop_client.ui.contracts import (
    ProviderPreflightCheck,
    ProviderPreflightName,
    ProviderPreflightState,
)

_CHECK_ORDER: tuple[ProviderPreflightName, ...] = (
    "tts_service",
    "tts_preset",
    "tts_reference",
    "vts_service",
    "vts_authentication",
    "vts_model",
    "vts_hotkeys",
)
_TTS_CHECKS: tuple[ProviderPreflightName, ...] = (
    "tts_service",
    "tts_preset",
    "tts_reference",
)
_VTS_CHECKS: tuple[ProviderPreflightName, ...] = (
    "vts_service",
    "vts_authentication",
    "vts_model",
    "vts_hotkeys",
)
_TTS_TEST_TEXT = "连接测试"


class TTSPreflightProvider(Protocol):
    async def probe(self, *, timeout_ms: int) -> GPTSoVITSProbe: ...

    async def synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult: ...

    async def discard(self, result: AudioResult) -> None: ...

    async def close(self) -> None: ...


class VTSPreflightBridge(Protocol):
    def start(self) -> None: ...

    async def close(self) -> None: ...


TTSFactory = Callable[[Settings], TTSPreflightProvider]
VTSStateListener = Callable[[VTSBridgeSnapshot], None]
VTSFactory = Callable[[Settings, VTSStateListener], VTSPreflightBridge]
PreflightPublisher = Callable[[tuple[ProviderPreflightCheck, ...]], None]


class ProviderPreflightRunner:
    """Run one user-requested, non-played end-to-end provider check."""

    def __init__(
        self,
        *,
        temp_registry: TempAssetRegistry | None = None,
        tts_factory: TTSFactory | None = None,
        vts_factory: VTSFactory | None = None,
    ) -> None:
        self._temp_registry = temp_registry
        self._tts_factory = tts_factory or self._build_tts
        self._vts_factory = vts_factory or self._build_vts

    async def run(
        self,
        settings: Settings,
        *,
        publish: PreflightPublisher,
    ) -> tuple[ProviderPreflightCheck, ...]:
        checks = {
            name: ProviderPreflightCheck(name=name, state=ProviderPreflightState.pending)
            for name in _CHECK_ORDER
        }

        def update(*items: ProviderPreflightCheck) -> None:
            for item in items:
                checks[item.name] = item
            publish(tuple(checks[name] for name in _CHECK_ORDER))

        update(
            ProviderPreflightCheck(
                name="tts_service",
                state=ProviderPreflightState.running,
            ),
            ProviderPreflightCheck(
                name="vts_service",
                state=ProviderPreflightState.running,
            ),
        )
        await asyncio.gather(
            self._run_tts(settings, update),
            self._run_vts(settings, update),
        )
        return tuple(checks[name] for name in _CHECK_ORDER)

    async def _run_tts(
        self,
        settings: Settings,
        update: Callable[..., None],
    ) -> None:
        provider_name = settings.tts.provider.strip().casefold()
        if provider_name == "mock":
            update(
                *(
                    ProviderPreflightCheck(
                        name=name,
                        state=ProviderPreflightState.skipped,
                        reason_code="tts_mock_active",
                    )
                    for name in _TTS_CHECKS
                )
            )
            return
        if provider_name not in {"gpt-sovits", "gpt_sovits"}:
            update(
                ProviderPreflightCheck(
                    name="tts_service",
                    state=ProviderPreflightState.failed,
                    reason_code="tts_provider_unsupported",
                ),
                ProviderPreflightCheck(
                    name="tts_preset",
                    state=ProviderPreflightState.skipped,
                    reason_code="tts_provider_unsupported",
                ),
                ProviderPreflightCheck(
                    name="tts_reference",
                    state=ProviderPreflightState.skipped,
                    reason_code="tts_provider_unsupported",
                ),
            )
            return
        if settings.tts.default_preset not in settings.tts.presets:
            update(
                ProviderPreflightCheck(
                    name="tts_service",
                    state=ProviderPreflightState.skipped,
                    reason_code="tts_preset_required",
                ),
                ProviderPreflightCheck(
                    name="tts_preset",
                    state=ProviderPreflightState.failed,
                    reason_code="tts_preset_required",
                ),
                ProviderPreflightCheck(
                    name="tts_reference",
                    state=ProviderPreflightState.failed,
                    reason_code="tts_reference_required",
                ),
            )
            return

        provider: TTSPreflightProvider | None = None
        try:
            provider = self._tts_factory(settings)
        except Exception:
            update(
                ProviderPreflightCheck(
                    name="tts_service",
                    state=ProviderPreflightState.skipped,
                    reason_code="tts_reference_unavailable",
                ),
                ProviderPreflightCheck(
                    name="tts_preset",
                    state=ProviderPreflightState.failed,
                    reason_code="tts_preset_unavailable",
                ),
                ProviderPreflightCheck(
                    name="tts_reference",
                    state=ProviderPreflightState.failed,
                    reason_code="tts_reference_unavailable",
                ),
            )
            return

        service_ready = False
        try:
            probe_timeout_ms = max(1, round(settings.tts.connect_timeout_seconds * 1_000))
            probe = await provider.probe(timeout_ms=probe_timeout_ms)
            if not probe.available:
                reason = probe.error_code or "tts_unavailable"
                update(
                    ProviderPreflightCheck(
                        name="tts_service",
                        state=ProviderPreflightState.failed,
                        reason_code=reason,
                    ),
                    ProviderPreflightCheck(
                        name="tts_preset",
                        state=ProviderPreflightState.skipped,
                        reason_code=reason,
                    ),
                    ProviderPreflightCheck(
                        name="tts_reference",
                        state=ProviderPreflightState.skipped,
                        reason_code=reason,
                    ),
                )
                return
            service_ready = True
            update(
                ProviderPreflightCheck(
                    name="tts_service",
                    state=ProviderPreflightState.ready,
                ),
                ProviderPreflightCheck(
                    name="tts_preset",
                    state=ProviderPreflightState.running,
                ),
                ProviderPreflightCheck(
                    name="tts_reference",
                    state=ProviderPreflightState.running,
                ),
            )
            token = CancellationToken("provider-preflight")
            result = await provider.synthesize(
                TTSJob(
                    turn_id="provider_preflight",
                    segment_id="provider_preflight",
                    text=_TTS_TEST_TEXT,
                    style=settings.tts.default_preset,
                    connect_timeout_ms=max(1, round(settings.tts.connect_timeout_seconds * 1_000)),
                    first_byte_timeout_ms=max(
                        1, round(settings.tts.first_byte_timeout_seconds * 1_000)
                    ),
                    timeout_ms=max(1, round(settings.tts.timeout_seconds * 1_000)),
                    cancellation_timeout_ms=max(
                        1,
                        round(settings.tts.cancellation_timeout_seconds * 1_000),
                    ),
                    cancellation_token_id=token.token_id,
                ),
                segment_index=0,
                token=token,
            )
            if not result.success:
                update(
                    ProviderPreflightCheck(
                        name="tts_preset",
                        state=ProviderPreflightState.failed,
                        reason_code="tts_preset_unavailable",
                    ),
                    ProviderPreflightCheck(
                        name="tts_reference",
                        state=ProviderPreflightState.failed,
                        reason_code="tts_reference_unavailable",
                    ),
                )
                return
            await provider.discard(result)
            update(
                ProviderPreflightCheck(
                    name="tts_preset",
                    state=ProviderPreflightState.ready,
                ),
                ProviderPreflightCheck(
                    name="tts_reference",
                    state=ProviderPreflightState.ready,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if service_ready:
                update(
                    ProviderPreflightCheck(
                        name="tts_preset",
                        state=ProviderPreflightState.failed,
                        reason_code="tts_preset_unavailable",
                    ),
                    ProviderPreflightCheck(
                        name="tts_reference",
                        state=ProviderPreflightState.failed,
                        reason_code="tts_reference_unavailable",
                    ),
                )
            else:
                update(
                    ProviderPreflightCheck(
                        name="tts_service",
                        state=ProviderPreflightState.failed,
                        reason_code="tts_unavailable",
                    ),
                    ProviderPreflightCheck(
                        name="tts_preset",
                        state=ProviderPreflightState.skipped,
                        reason_code="tts_unavailable",
                    ),
                    ProviderPreflightCheck(
                        name="tts_reference",
                        state=ProviderPreflightState.skipped,
                        reason_code="tts_unavailable",
                    ),
                )
        finally:
            with suppress(Exception):
                await provider.close()

    async def _run_vts(
        self,
        settings: Settings,
        update: Callable[..., None],
    ) -> None:
        if not settings.vts.enabled:
            update(
                *(
                    ProviderPreflightCheck(
                        name=name,
                        state=ProviderPreflightState.skipped,
                        reason_code="vts_disabled",
                    )
                    for name in _VTS_CHECKS
                )
            )
            return

        terminal = asyncio.Event()

        def state_listener(snapshot: VTSBridgeSnapshot) -> None:
            update(*_vts_checks(snapshot))
            if snapshot.state in {
                VTSBridgeState.ready,
                VTSBridgeState.disabled,
                VTSBridgeState.backoff,
            }:
                terminal.set()

        bridge: VTSPreflightBridge | None = None
        try:
            bridge = self._vts_factory(settings, state_listener)
            bridge.start()
            timeout = max(
                5.0,
                min(60.0, settings.vts.request_timeout_seconds * 5),
            )
            await asyncio.wait_for(terminal.wait(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            update(
                ProviderPreflightCheck(
                    name="vts_service",
                    state=ProviderPreflightState.failed,
                    reason_code="vts_timeout",
                ),
                *(
                    ProviderPreflightCheck(
                        name=name,
                        state=ProviderPreflightState.skipped,
                        reason_code="vts_timeout",
                    )
                    for name in _VTS_CHECKS[1:]
                ),
            )
        except Exception:
            update(
                ProviderPreflightCheck(
                    name="vts_service",
                    state=ProviderPreflightState.failed,
                    reason_code="vts_unavailable",
                ),
                *(
                    ProviderPreflightCheck(
                        name=name,
                        state=ProviderPreflightState.skipped,
                        reason_code="vts_unavailable",
                    )
                    for name in _VTS_CHECKS[1:]
                ),
            )
        finally:
            if bridge is not None:
                with suppress(Exception):
                    await bridge.close()

    def _build_tts(self, settings: Settings) -> GPTSoVITSProvider:
        presets: Mapping[str, GPTSoVITSPreset] = {
            name: GPTSoVITSPreset(**preset.model_dump())
            for name, preset in settings.tts.presets.items()
        }
        return GPTSoVITSProvider(
            settings.tts.base_url,
            settings.tts_output_directory(),
            presets,
            default_preset=settings.tts.default_preset,
            max_audio_bytes=settings.tts.max_audio_bytes,
            cache_enabled=False,
            proxy_url=settings.tts.transport.proxy_url,
            ca_bundle_path=settings.tts_ca_bundle_path(),
            max_owned_synthesis_tasks=1,
            temp_registry=self._temp_registry,
        )

    @staticmethod
    def _build_vts(
        settings: Settings,
        state_listener: VTSStateListener,
    ) -> VTSBridge:
        mapper = (
            ExpressionMapper(settings.vts.expression_hotkeys)
            if settings.vts.expression_hotkeys
            else ExpressionMapper()
        )
        return VTSBridge(
            lambda: VTSClient(
                settings.vts.uri,
                request_timeout_seconds=settings.vts.request_timeout_seconds,
                proxy_url=settings.vts.transport.proxy_url,
                ca_bundle_path=settings.vts_ca_bundle_path(),
            ),
            DPAPITokenStore(
                vts_token_file(
                    settings.paths,
                    settings.vts_token_path(),
                )
            ),
            plugin_name=settings.vts.plugin_name,
            plugin_developer=settings.vts.plugin_developer,
            expression_mapper=mapper,
            queue_capacity=settings.vts.queue_capacity,
            reconnect_initial_seconds=settings.vts.reconnect_initial_seconds,
            reconnect_max_seconds=settings.vts.reconnect_max_seconds,
            state_listener=state_listener,
        )


def _vts_checks(snapshot: VTSBridgeSnapshot) -> tuple[ProviderPreflightCheck, ...]:
    state = snapshot.state
    reason = snapshot.error_code
    if state is VTSBridgeState.connecting:
        return (
            _check("vts_service", ProviderPreflightState.running),
            _check("vts_authentication", ProviderPreflightState.pending),
            _check("vts_model", ProviderPreflightState.pending),
            _check("vts_hotkeys", ProviderPreflightState.pending),
        )
    if state is VTSBridgeState.authorizing:
        return (
            _check(
                "vts_service",
                (
                    ProviderPreflightState.ready
                    if snapshot.api_available
                    else ProviderPreflightState.running
                ),
            ),
            _check(
                "vts_authentication",
                ProviderPreflightState.action_required,
                "vts_allow_or_auth_required",
            ),
            _check("vts_model", ProviderPreflightState.pending),
            _check("vts_hotkeys", ProviderPreflightState.pending),
        )
    if state is VTSBridgeState.preflighting:
        return (
            _check("vts_service", ProviderPreflightState.ready),
            _check("vts_authentication", ProviderPreflightState.ready),
            _check("vts_model", ProviderPreflightState.running),
            _check("vts_hotkeys", ProviderPreflightState.running),
        )
    if state is VTSBridgeState.ready:
        return (
            _check("vts_service", ProviderPreflightState.ready),
            _check("vts_authentication", ProviderPreflightState.ready),
            _check("vts_model", ProviderPreflightState.ready),
            _check(
                "vts_hotkeys",
                ProviderPreflightState.ready,
                missing_count=snapshot.missing_hotkey_count,
            ),
        )
    if state is VTSBridgeState.backoff:
        return (
            _check(
                "vts_service",
                ProviderPreflightState.reconnecting,
                reason or "vts_disconnected",
            ),
            _check(
                "vts_authentication",
                ProviderPreflightState.skipped,
                reason or "vts_disconnected",
            ),
            _check(
                "vts_model",
                ProviderPreflightState.skipped,
                reason or "vts_disconnected",
            ),
            _check(
                "vts_hotkeys",
                ProviderPreflightState.skipped,
                reason or "vts_disconnected",
            ),
        )
    if state is VTSBridgeState.disabled:
        return _disabled_vts_checks(snapshot)
    return (
        _check("vts_service", ProviderPreflightState.pending),
        _check("vts_authentication", ProviderPreflightState.pending),
        _check("vts_model", ProviderPreflightState.pending),
        _check("vts_hotkeys", ProviderPreflightState.pending),
    )


def _disabled_vts_checks(
    snapshot: VTSBridgeSnapshot,
) -> tuple[ProviderPreflightCheck, ...]:
    reason = snapshot.error_code or "vts_config"
    service_ready = snapshot.api_available
    auth_ready = snapshot.authenticated
    model_ready = snapshot.model_loaded
    hotkey_failed = reason == "vts_hotkey_missing"
    return (
        _check(
            "vts_service",
            ProviderPreflightState.ready if service_ready else ProviderPreflightState.failed,
            None if service_ready else reason,
        ),
        _check(
            "vts_authentication",
            (
                ProviderPreflightState.ready
                if auth_ready
                else (
                    ProviderPreflightState.failed
                    if service_ready
                    else ProviderPreflightState.skipped
                )
            ),
            None if auth_ready else reason,
        ),
        _check(
            "vts_model",
            (
                ProviderPreflightState.ready
                if model_ready
                else (
                    ProviderPreflightState.failed
                    if auth_ready and reason != "vts_hotkey_missing"
                    else ProviderPreflightState.skipped
                )
            ),
            None if model_ready else reason,
        ),
        _check(
            "vts_hotkeys",
            (
                ProviderPreflightState.failed
                if model_ready or hotkey_failed
                else ProviderPreflightState.skipped
            ),
            reason,
            missing_count=snapshot.missing_hotkey_count,
        ),
    )


def _check(
    name: ProviderPreflightName,
    state: ProviderPreflightState,
    reason_code: str | None = None,
    *,
    missing_count: int = 0,
) -> ProviderPreflightCheck:
    return ProviderPreflightCheck(
        name=name,
        state=state,
        reason_code=reason_code,
        missing_count=missing_count,
    )
