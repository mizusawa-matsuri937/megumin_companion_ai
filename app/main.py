"""Side-effect-free FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.api.security import DevAPIConfig, DevAPIGuardMiddleware, DevAPISecurity
from app.bootstrap import (
    build_dialogue_pipeline,
    build_llm_provider,
    build_vts_event_sink,
)
from app.clients.llm.base import LLMProvider
from app.config import Settings, load_settings
from app.config.logging import configure_logging, log_event
from app.core import TurnService
from app.core.idempotency import UnavailableIdempotencyStore
from app.memory.analyzer import LLMMemoryCandidateAnalyzer
from app.memory.runtime import MemoryRuntime, create_memory_runtime
from app.proactive import ProactivePolicy, ProactiveRuntime
from app.runtime_storage import prepare_runtime_storage
from app.schemas import utc_now
from app.storage import SQLiteIdempotencyStore


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
) -> FastAPI:
    resolved_settings = settings or load_settings()
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
        app.state.idempotency_store = None
        app.state.temp_asset_registry = runtime_storage.temp_registry
        standalone_analyzer_provider: LLMProvider | None = None
        try:
            memory_runtime: MemoryRuntime | None = None
            if resolved_settings.storage.enabled:
                analyzer = None
                if resolved_settings.memory.candidate_analysis_enabled:
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
            idempotency_store = (
                SQLiteIdempotencyStore(memory_runtime.database)
                if memory_runtime is not None
                else UnavailableIdempotencyStore()
            )
            if memory_runtime is not None:
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
                if memory_runtime is not None
                else None
            )
            app.state.proactive_runtime = proactive_runtime
            if memory_runtime is not None and proactive_runtime is not None:
                memory_runtime.add_feature_transition_handler(proactive_runtime.apply_feature_state)
            app.state.vts_event_sink = build_vts_event_sink(resolved_settings)
            event_sinks = (
                (app.state.vts_event_sink,) if app.state.vts_event_sink is not None else ()
            )
            observers = (memory_runtime.observer,) if memory_runtime is not None else ()
            turn_service = TurnService(
                logger,
                build_dialogue_pipeline(
                    resolved_settings,
                    prompt_context_source=(
                        memory_runtime.context_source if memory_runtime is not None else None
                    ),
                    temp_registry=runtime_storage.temp_registry,
                ),
                observers=observers,
                event_sinks=event_sinks,
                priority_controller=proactive_runtime,
                idempotency_store=idempotency_store,
            )
            app.state.turn_service = turn_service
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
        finally:
            proactive = getattr(app.state, "proactive_runtime", None)
            service = getattr(app.state, "turn_service", None)
            sink = getattr(app.state, "vts_event_sink", None)
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
            if isinstance(runtime, MemoryRuntime):
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
                for handler in logger.handlers:
                    handler.flush()
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
