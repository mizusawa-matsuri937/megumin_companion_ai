"""Private gateway process entrypoint; no WebUI or upstream API is exposed."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import uvicorn

from app.paths import AppPaths
from app.secret_store import tts_gateway_token_file
from app.tts_gateway.engine import GatewayEngine
from app.tts_gateway.manifest import load_gateway_manifest
from app.tts_gateway.official_backend import OfficialGPTSoVITSBackend
from app.tts_gateway.server import create_gateway_app

_OFFLINE_ENVIRONMENT = {
    "DO_NOT_TRACK": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    _enforce_offline_environment()

    manifest, root = load_gateway_manifest(arguments.manifest)
    token_file = tts_gateway_token_file(AppPaths.discover())
    token = token_file.read_text()
    if token is None:
        raise RuntimeError("tts_gateway_secret_missing")
    backend = OfficialGPTSoVITSBackend(manifest, root)
    engine = GatewayEngine(manifest, backend)

    async def ready() -> None:
        await engine.start()

    asyncio.run(ready())
    application = create_gateway_app(engine, token)
    uvicorn.run(
        application,
        host="127.0.0.1",
        port=9880,
        workers=1,
        access_log=False,
        log_level="warning",
        server_header=False,
    )
    return 0


def _enforce_offline_environment() -> None:
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    for name, value in _OFFLINE_ENVIRONMENT.items():
        os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
