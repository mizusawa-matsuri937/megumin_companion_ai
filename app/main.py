"""FastAPI application factory and local development entry point."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger = configure_logging(resolved_settings)
        app.state.settings = resolved_settings
        app.state.logger = logger
        app.state.memory_runtime = None
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
            app.state.vts_event_sink = build_vts_event_sink(resolved_settings)
            event_sinks = (
                (app.state.vts_event_sink,) if app.state.vts_event_sink is not None else ()
            )
            observers = (memory_runtime.observer,) if memory_runtime is not None else ()
            app.state.turn_service = TurnService(
                logger,
                build_dialogue_pipeline(
                    resolved_settings,
                    prompt_context_source=(
                        memory_runtime.context_source if memory_runtime is not None else None
                    ),
                ),
                observers=observers,
                event_sinks=event_sinks,
            )
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
            service = getattr(app.state, "turn_service", None)
            if isinstance(service, TurnService):
                await service.shutdown()
            runtime = getattr(app.state, "memory_runtime", None)
            if isinstance(runtime, MemoryRuntime):
                await runtime.close()
            elif standalone_analyzer_provider is not None:
                await standalone_analyzer_provider.close()
            log_event(logger, logging.INFO, "application.stopped")
            for handler in logger.handlers:
                handler.flush()

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
