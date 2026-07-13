"""FastAPI application factory and local development entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app import __version__  # noqa: E402
from app.api.routes import router  # noqa: E402
from app.bootstrap import (  # noqa: E402
    build_dialogue_pipeline,
    build_llm_provider,
    build_vts_event_sink,
)
from app.clients.llm.base import LLMProvider  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.config.logging import configure_logging, log_event  # noqa: E402
from app.config.settings import PROJECT_ROOT  # noqa: E402
from app.core import TurnService  # noqa: E402
from app.memory.analyzer import LLMMemoryCandidateAnalyzer  # noqa: E402
from app.memory.runtime import MemoryRuntime, create_memory_runtime  # noqa: E402
from app.proactive import ProactivePolicy, ProactiveRuntime  # noqa: E402


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


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger = configure_logging(resolved_settings)
        app.state.settings = resolved_settings
        app.state.logger = logger
        app.state.memory_runtime = None
        app.state.proactive_runtime = None
        app.state.turn_service = None
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
                database_path = resolved_settings.storage.database_path
                if not database_path.is_absolute():
                    database_path = PROJECT_ROOT / database_path
                memory_runtime = await create_memory_runtime(
                    str(database_path),
                    busy_timeout_ms=resolved_settings.storage.busy_timeout_ms,
                    retention_days=resolved_settings.memory.history_retention_days,
                    confirmation_ttl_minutes=(resolved_settings.memory.confirmation_ttl_minutes),
                    analyzer=analyzer,
                )
                standalone_analyzer_provider = None
            app.state.memory_runtime = memory_runtime
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
                ),
                observers=observers,
                event_sinks=event_sinks,
                priority_controller=proactive_runtime,
            )
            app.state.turn_service = turn_service
            if proactive_runtime is not None:
                proactive_runtime.start(turn_service.run_proactive)
            log_event(
                logger,
                logging.INFO,
                "application.started",
                environment=resolved_settings.app.environment,
                host=resolved_settings.server.host,
                port=resolved_settings.server.port,
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
    )
    app.include_router(router)

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


app = create_app()


def run() -> None:
    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
    )


if __name__ == "__main__":
    run()
