"""Run inside an isolated wheel environment to verify W01 packaging contracts."""

from __future__ import annotations

import argparse
import asyncio
import json
from importlib import metadata, resources
from pathlib import Path

import app
import app.main as main_module
import httpx
from app.cli import check_configuration


async def _health_payload() -> tuple[int, dict[str, object]]:
    application = main_module.create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://installed") as client:
            response = await client.get("/health")
    return response.status_code, response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forbid-root", type=Path, required=True)
    args = parser.parse_args()

    package_path = Path(app.__file__).resolve()
    forbidden = args.forbid_root.resolve()
    if package_path.is_relative_to(forbidden):
        raise RuntimeError("smoke imported the source tree instead of the installed wheel")
    if "app" in vars(main_module):
        raise RuntimeError("app.main constructed a global ASGI application during import")

    default_config = (
        resources.files("app.resources").joinpath("default_config.yaml").read_text(encoding="utf-8")
    )
    if "playback_mode: silent" not in default_config:
        raise RuntimeError("packaged default configuration is missing or unexpected")

    console_scripts = {item.name for item in metadata.entry_points(group="console_scripts")}
    gui_scripts = {item.name for item in metadata.entry_points(group="gui_scripts")}
    if "megumin-companion-api" not in console_scripts:
        raise RuntimeError("console entry point is missing")
    if "megumin-companion-desktop" not in gui_scripts:
        raise RuntimeError("desktop entry point is missing")

    if check_configuration(None, Path.cwd() / "missing.env") != 0:
        raise RuntimeError("packaged configuration check failed")

    health_status, health_payload = asyncio.run(_health_payload())
    if health_status != 200 or health_payload.get("status") != "ok":
        raise RuntimeError("installed ASGI health smoke failed")

    print(
        json.dumps(
            {
                "status": "ok",
                "package": str(package_path),
                "version": metadata.version("megumin-companion-ai"),
                "resource_bytes": len(default_config.encode("utf-8")),
                "health": health_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
