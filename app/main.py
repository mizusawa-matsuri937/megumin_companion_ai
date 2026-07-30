"""Side-effect-free FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.api.security import (
    DevAPIConfig,
    DevAPIGuardMiddleware,
    DevAPISecurity,
    apply_hard_limits,
)
from app.avatar import AvatarTurnEventSink
from app.bootstrap import (
    build_avatar_runtime,
    build_dialogue_pipeline,
    build_llm_provider,
    build_vts_event_sink,
)
from app.clients.llm.base import LLMProvider
from app.config import Settings, load_settings
from app.config.logging import close_logging, configure_logging, log_event
from app.core import TurnService
from app.core.idempotency import UnavailableIdempotencyStore
from app.diagnostics import write_crash_report
from app.health import (
    CapabilityCheck,
    CapabilityState,
    HealthAggregator,
    HealthProvider,
)
from app.memory.analyzer import LLMMemoryCandidateAnalyzer
from app.memory.runtime import MemoryRuntime, SafeModeMemoryRuntime, create_memory_runtime
from app.perception.guards import TextRedactor
from app.perception.prompt_context import ApprovedVisualSummary, PerceptionPromptContextSource
from app.proactive import ProactivePolicy, ProactiveRuntime
from app.prompts import CompositePromptContextSource
from app.runtime_storage import prepare_runtime_storage
from app.schemas import FeatureName, PerceptionContext, utc_now
from app.storage import SQLiteIdempotencyStore


class _CoreHealthProvider:
    name = "core"
    required_for_readiness = True

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def check_health(self) -> CapabilityCheck:
        if isinstance(getattr(self._app.state, "turn_service", None), TurnService):
            return CapabilityCheck(status=CapabilityState.ready)
        return CapabilityCheck(
            status=CapabilityState.unavailable,
            error_code="service_not_ready",
        )


class _IdempotencyHealthProvider:
    """Expose the real W06 composition boundary without probing private records."""

    name = "idempotency"
    required_for_readiness = True

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def check_health(self) -> CapabilityCheck:
        store = getattr(self._app.state, "idempotency_store", None)
        if isinstance(store, SQLiteIdempotencyStore):
            return CapabilityCheck(status=CapabilityState.ready)
        return CapabilityCheck(
            status=CapabilityState.unavailable,
            error_code="idempotency_unavailable",
        )


async def _settle_resource_close(
    closer: Callable[[], Awaitable[None]],
) -> tuple[asyncio.CancelledError | None, Exception | None]:
    """Drain one shared closer despite repeated cancellation of the lifespan task."""

    try:
        task = asyncio.ensure_future(closer())
    except Exception as exc:
        return None, exc
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
        except Exception:
            break
    try:
        task.result()
    except asyncio.CancelledError as exc:
        if cancelled is None:
            cancelled = exc
    except Exception as exc:
        return cancelled, exc
    return cancelled, None


def create_app(
    settings: Settings | None = None,
    *,
    dev_api: DevAPIConfig | None = None,
    health_providers: Sequence[HealthProvider] = (),
    safe_mode: bool = False,
    allow_deepseek_env_fallback: bool = False,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_settings.validate_runtime_limits()
    if allow_deepseek_env_fallback and dev_api is None:
        raise ValueError("DEEPSEEK_API_KEY 回退仅可用于显式 --dev-api 运行面。")
    if dev_api is not None:
        dev_api = apply_hard_limits(dev_api, resolved_settings.limits)
    dev_api_security = DevAPISecurity(dev_api) if dev_api is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime_storage = prepare_runtime_storage(resolved_settings.paths)
        logger = configure_logging(
            resolved_settings,
            additional_secrets=(dev_api.token, dev_api.client_id, dev_api.session_id)
            if dev_api is not None
            else (),
        )
        if dev_api_security is not None:
            dev_api_security.set_logger(logger)
        app.state.settings = resolved_settings
        app.state.logger = logger
        app.state.memory_runtime = None
        app.state.proactive_runtime = None
        app.state.turn_service = None
        app.state.health = None
        app.state.idempotency_store = None
        app.state.avatar_runtime = None
        app.state.vts_event_sink = None
        app.state.perception_context_source = None
        app.state.publish_perception = None
        app.state.publish_approved_visual_summary = None
        app.state.temp_asset_registry = runtime_storage.temp_registry
        standalone_analyzer_provider: LLMProvider | None = None
        try:
            memory_runtime: MemoryRuntime | SafeModeMemoryRuntime | None = None
            if resolved_settings.storage.enabled:
                analyzer = None
                if resolved_settings.memory.candidate_analysis_enabled:
                    if resolved_settings.llm.provider.strip().casefold() == "deepseek":
                        raise RuntimeError(
                            "DeepSeek Flash 不能用于长期记忆候选写入；请先关闭候选分析。"
                        )
                    standalone_analyzer_provider = build_llm_provider(resolved_settings)
                    if standalone_analyzer_provider is None:
                        raise RuntimeError("记忆候选分析已启用，但没有配置可用的 LLM provider。")
                    analyzer = LLMMemoryCandidateAnalyzer(
                        standalone_analyzer_provider,
                        owns_provider=True,
                    )
                database_path = resolved_settings.database_path()
                memory_runtime = await create_memory_runtime(
                    str(database_path),
                    busy_timeout_ms=resolved_settings.storage.busy_timeout_ms,
                    retention_days=resolved_settings.memory.history_retention_days,
                    confirmation_ttl_minutes=(resolved_settings.memory.confirmation_ttl_minutes),
                    analyzer=analyzer,
                )
                standalone_analyzer_provider = None
            app.state.memory_runtime = memory_runtime
            if isinstance(memory_runtime, MemoryRuntime) and safe_mode:
                # An interrupted desktop lifetime must never silently restore
                # capture/proactive work.  Persisting ``disabled`` makes the
                # recovery state explicit; a later settings UI can require an
                # affirmative user action before either feature runs again.
                for feature_name in (
                    FeatureName.vision,
                    FeatureName.cloud_vision,
                    FeatureName.proactive,
                ):
                    await memory_runtime.set_feature(feature_name, False)
            idempotency_store = (
                SQLiteIdempotencyStore(memory_runtime.database)
                if isinstance(memory_runtime, MemoryRuntime)
                else UnavailableIdempotencyStore()
            )
            if isinstance(memory_runtime, MemoryRuntime):
                await idempotency_store.recover_incomplete(now=utc_now())
            app.state.idempotency_store = idempotency_store
            proactive_runtime = (
                ProactiveRuntime(
                    memory_runtime.features,
                    ProactivePolicy(
                        timezone=resolved_settings.app.timezone,
                        minimum_score=resolved_settings.proactive.minimum_score,
                        cooldown=timedelta(seconds=resolved_settings.proactive.cooldown_seconds),
                        idle_minimum=timedelta(
                            seconds=resolved_settings.proactive.idle_minimum_seconds
                        ),
                        perception_max_age=timedelta(
                            seconds=resolved_settings.proactive.perception_max_age_seconds
                        ),
                        daily_limit=resolved_settings.proactive.daily_limit,
                        quiet_start_hour=resolved_settings.proactive.quiet_start_hour,
                        quiet_end_hour=resolved_settings.proactive.quiet_end_hour,
                    ),
                )
                if isinstance(memory_runtime, MemoryRuntime)
                else None
            )
            app.state.proactive_runtime = proactive_runtime
            perception_context_source = (
                PerceptionPromptContextSource(
                    memory_runtime.features,
                    max_age=timedelta(
                        seconds=resolved_settings.perception.prompt_context_max_age_seconds
                    ),
                    redactor=TextRedactor(max_chars=resolved_settings.perception.max_summary_chars),
                )
                if isinstance(memory_runtime, MemoryRuntime)
                else None
            )
            app.state.perception_context_source = perception_context_source
            if isinstance(memory_runtime, MemoryRuntime) and proactive_runtime is not None:
                memory_runtime.add_feature_transition_handler(proactive_runtime.apply_feature_state)
            if isinstance(memory_runtime, MemoryRuntime) and perception_context_source is not None:
                memory_runtime.add_feature_transition_handler(
                    perception_context_source.apply_feature_state
                )

                async def publish_perception(context: PerceptionContext | None) -> None:
                    """Keep raw perception out of prompts while preserving proactive input."""

                    if proactive_runtime is not None:
                        await proactive_runtime.update_perception(context)

                async def publish_approved_visual_summary(
                    approved: ApprovedVisualSummary | None,
                ) -> None:
                    """Accept only the future privacy-bound prompt capability."""

                    perception_context_source.publish_approved(approved)

                app.state.publish_perception = publish_perception
                app.state.publish_approved_visual_summary = publish_approved_visual_summary
            avatar_runtime = build_avatar_runtime(resolved_settings)
            app.state.avatar_runtime = avatar_runtime
            if avatar_runtime is not None:
                avatar_sink = AvatarTurnEventSink(avatar_runtime)
                app.state.vts_event_sink = avatar_sink
                avatar_sink.start()
            else:
                app.state.vts_event_sink = build_vts_event_sink(resolved_settings)
            event_sinks = (
                (app.state.vts_event_sink,) if app.state.vts_event_sink is not None else ()
            )
            observers = (
                (memory_runtime.observer,) if isinstance(memory_runtime, MemoryRuntime) else ()
            )
            turn_service = TurnService(
                logger,
                build_dialogue_pipeline(
                    resolved_settings,
                    prompt_context_source=(
                        CompositePromptContextSource(
                            memory_runtime.context_source,
                            perception_context_source,
                        )
                        if isinstance(memory_runtime, MemoryRuntime)
                        and perception_context_source is not None
                        else None
                    ),
                    temp_registry=runtime_storage.temp_registry,
                    mouth_envelope_listener=(
                        avatar_runtime.offer_mouth_envelope if avatar_runtime is not None else None
                    ),
                    allow_deepseek_env_fallback=allow_deepseek_env_fallback,
                ),
                observers=observers,
                event_sinks=event_sinks,
                priority_controller=proactive_runtime,
                idempotency_store=idempotency_store,
            )
            app.state.turn_service = turn_service
            app.state.health = HealthAggregator(
                service="megumin-companion-ai",
                version=__version__,
                providers=(
                    _CoreHealthProvider(app),
                    _IdempotencyHealthProvider(app),
                    *((memory_runtime,) if memory_runtime is not None else ()),
                    *((avatar_runtime,) if avatar_runtime is not None else ()),
                    *health_providers,
                ),
            )
            if proactive_runtime is not None:
                proactive_runtime.start(turn_service.run_proactive)
            log_event(
                logger,
                logging.INFO,
                "application.started",
                environment=resolved_settings.app.environment,
                dev_api_enabled=dev_api_security is not None,
                dev_api_authorities=(sorted(dev_api.allowed_hosts) if dev_api is not None else []),
                temp_deleted=runtime_storage.scavenge_report.deleted,
                temp_pending=runtime_storage.scavenge_report.pending,
                temp_rejected=runtime_storage.scavenge_report.rejected,
            )
            yield
        except Exception as exc:
            with suppress(Exception):
                write_crash_report(
                    resolved_settings.paths,
                    error_code="application_runtime_failure",
                    exception=exc,
                    file_count=resolved_settings.logging.file_count,
                    retention_days=resolved_settings.logging.retention_days,
                )
            raise
        finally:
            proactive = getattr(app.state, "proactive_runtime", None)
            service = getattr(app.state, "turn_service", None)
            sink = getattr(app.state, "vts_event_sink", None)
            avatar = getattr(app.state, "avatar_runtime", None)
            runtime = getattr(app.state, "memory_runtime", None)
            cancelled: asyncio.CancelledError | None = None
            resources: list[tuple[str, Callable[[], Awaitable[None]]]] = []
            if isinstance(proactive, ProactiveRuntime):
                resources.append(("proactive", proactive.close))
            if isinstance(service, TurnService):
                resources.append(("turn_service", service.shutdown))
            if sink is not None:
                # TurnService normally owns the sink; the second idempotent close
                # also covers partial startup and an unexpected service-close error.
                resources.append(("vts_event_sink", sink.close))
            if avatar is not None:
                # The sink normally owns the runtime; this third idempotent close
                # also covers failure between runtime construction and sink startup.
                resources.append(("avatar_runtime", avatar.close))
            if isinstance(runtime, MemoryRuntime | SafeModeMemoryRuntime):
                resources.append(("memory", runtime.close))
            elif standalone_analyzer_provider is not None:
                resources.append(("memory_analyzer_provider", standalone_analyzer_provider.close))
            try:
                for resource_name, closer in resources:
                    close_cancelled, failure = await _settle_resource_close(closer)
                    if cancelled is None and close_cancelled is not None:
                        cancelled = close_cancelled
                    if failure is not None:
                        log_event(
                            logger,
                            logging.ERROR,
                            "application.resource_close_failed",
                            resource=resource_name,
                        )
                log_event(logger, logging.INFO, "application.stopped")
            finally:
                close_logging(logger)
            if cancelled is not None:
                raise cancelled

    app = FastAPI(
        title=resolved_settings.app.name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(router)
    app.add_middleware(DevAPIGuardMiddleware, security=dev_api_security)

    @app.exception_handler(RequestValidationError)
    async def safe_request_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {key: value for key, value in error.items() if key not in {"input", "ctx", "url"}}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    return app
