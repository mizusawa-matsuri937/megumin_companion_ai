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
from app.clients.llm import MockLLMProvider  # noqa: E402
from app.clients.tts import MockTTSProvider  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.config.logging import configure_logging, log_event  # noqa: E402
from app.config.settings import PROJECT_ROOT  # noqa: E402
from app.core import TurnService  # noqa: E402
from app.pipelines import DialoguePipeline  # noqa: E402
from app.pipelines.audio_player import (  # noqa: E402
    AudioPlayer,
    SilentAudioPlayer,
    SystemAudioPlayer,
)


def _build_mock_pipeline(settings: Settings) -> DialoguePipeline | None:
    if settings.llm.provider.lower() == "none":
        return None
    if settings.llm.provider.lower() != "mock":
        raise RuntimeError(
            f"当前 Phase 1 只实现 mock provider，收到：{settings.llm.provider}。"
            "真实 OpenAI-compatible provider 将在 Day 10 接入。"
        )

    cache_path = settings.pipeline.audio_cache_path
    if not cache_path.is_absolute():
        cache_path = PROJECT_ROOT / cache_path
    llm = MockLLMProvider(token_delay_seconds=settings.pipeline.mock_token_delay_ms / 1000)
    tts = MockTTSProvider(
        cache_path,
        duration_ms=settings.pipeline.mock_audio_duration_ms,
        volume=settings.pipeline.mock_audio_volume,
    )
    player: AudioPlayer
    if settings.pipeline.playback_mode == "system":
        player = SystemAudioPlayer()
    else:
        player = SilentAudioPlayer()
    return DialoguePipeline(
        llm,
        tts,
        player,
        tts_worker_count=settings.pipeline.tts_worker_count,
        segment_min_chars=settings.pipeline.segment_min_chars,
        segment_max_chars=settings.pipeline.segment_max_chars,
        segment_max_words=settings.pipeline.segment_max_words,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger = configure_logging(resolved_settings)
        app.state.settings = resolved_settings
        app.state.logger = logger
        app.state.turn_service = TurnService(logger, _build_mock_pipeline(resolved_settings))
        log_event(
            logger,
            logging.INFO,
            "application.started",
            environment=resolved_settings.app.environment,
            host=resolved_settings.server.host,
            port=resolved_settings.server.port,
        )
        try:
            yield
        finally:
            await app.state.turn_service.shutdown()
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
