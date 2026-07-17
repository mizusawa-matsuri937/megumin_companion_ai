"""Explicit command-line entry point for configuration checks and the development API."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from app import __version__
from app.config import ConfigurationError, Settings, load_settings
from app.main import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megumin-companion-api",
        description=(
            "Validate packaged configuration or explicitly start the local development API. "
            "The Windows production desktop does not use this network entry point."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        help="Use an explicit YAML configuration instead of the packaged default.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Use an explicit development .env file; process environment values still win.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting the database, devices, or network.",
    )
    action.add_argument(
        "--serve",
        action="store_true",
        help="Explicitly start the current local development HTTP/WebSocket API.",
    )
    parser.add_argument("--host", help="Override the configured development API host.")
    parser.add_argument("--port", type=int, help="Override the configured development API port.")
    return parser


def _load_settings(config: Path | None, env_file: Path | None) -> Settings:
    return load_settings(config_path=config, env_path=env_file)


def check_configuration(config: Path | None, env_file: Path | None) -> int:
    """Validate and summarize configuration without constructing runtime resources."""

    try:
        settings = _load_settings(config, env_file)
    except ConfigurationError as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        return 2
    summary = {
        "status": "ok",
        "source": settings.config_source,
        "environment": settings.app.environment,
        "llm_provider": settings.llm.provider,
        "tts_provider": settings.tts.provider,
        "playback_mode": settings.pipeline.playback_mode,
        "storage_enabled": settings.storage.enabled,
        "vts_enabled": settings.vts.enabled,
        "stt_enabled": settings.stt.enabled,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def _run_server(application: FastAPI, *, host: str, port: int) -> None:
    uvicorn.run(application, host=host, port=port, log_config=None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check_config:
        return check_configuration(args.config, args.env_file)
    if not args.serve:
        parser.print_help()
        return 0

    try:
        settings = _load_settings(args.config, args.env_file)
    except ConfigurationError as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        return 2
    host = args.host or settings.server.host
    port = args.port if args.port is not None else settings.server.port
    if not 1 <= port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    _run_server(create_app(settings), host=host, port=port)
    return 0
