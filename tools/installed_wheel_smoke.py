"""Run outside the repository inside an isolated installed-wheel environment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
from importlib import metadata, resources
from pathlib import Path
from typing import Any


def _snapshot_directory(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[f"{relative}/"] = "directory"
        elif path.is_file():
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            snapshot[relative] = "unsupported"
    return snapshot


def _entry_point(name: str) -> Path:
    scripts = Path(sysconfig.get_path("scripts"))
    candidates = [scripts / name, scripts / f"{name}.exe", scripts / f"{name}-script.py"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"installed entry point is missing: {name}")


def _run_entry_point(name: str, *arguments: str, expected_return: int = 0) -> tuple[str, str]:
    entry_point = _entry_point(name)
    command = (
        [sys.executable, str(entry_point), *arguments]
        if entry_point.suffix.casefold() == ".py"
        else [str(entry_point), *arguments]
    )
    completed = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != expected_return:
        raise RuntimeError(
            f"{name} returned {completed.returncode}, expected {expected_return}: "
            f"{completed.stderr[-1000:]!r}"
        )
    return completed.stdout, completed.stderr


async def _health_payload(
    main_module: Any, dev_api_type: Any, httpx_module: Any
) -> tuple[int, int, str]:
    locked = main_module.create_app()
    locked_transport = httpx_module.ASGITransport(app=locked)
    async with httpx_module.AsyncClient(
        transport=locked_transport,
        base_url="http://127.0.0.1:8765",
    ) as locked_client:
        locked_response = await locked_client.get("/health")

    dev_api = dev_api_type(
        token="installed-wheel-smoke-token-0000000000000000",
        session_id="session_installed_wheel_smoke",
        allowed_origins=frozenset({"http://127.0.0.1:8765"}),
        allowed_hosts=frozenset({"127.0.0.1:8765"}),
    )
    application = main_module.create_app(dev_api=dev_api)
    async with application.router.lifespan_context(application):
        transport = httpx_module.ASGITransport(app=application)
        async with httpx_module.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
            headers=dev_api.client_headers(),
        ) as client:
            response = await client.get("/health")
    payload = response.json()
    return locked_response.status_code, response.status_code, str(payload.get("status", ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forbid-root", type=Path, required=True)
    parser.add_argument("--local-app-data", type=Path, required=True)
    args = parser.parse_args()
    forbidden = args.forbid_root.resolve()
    local_app_data = args.local_app_data.resolve()
    current_directory = Path.cwd().resolve()
    if current_directory.is_relative_to(forbidden):
        raise RuntimeError("installed smoke must execute outside the source repository")
    if local_app_data.is_relative_to(forbidden):
        raise RuntimeError("installed smoke LocalAppData must be outside the source repository")
    before_cwd = _snapshot_directory(current_directory)
    os.environ["LOCALAPPDATA"] = str(local_app_data)
    os.environ["XDG_DATA_HOME"] = str(local_app_data)

    import app
    import app.main as main_module
    import desktop_client
    import httpx
    from app.api.security import DevAPIConfig

    package_path = Path(app.__file__).resolve()
    if package_path.is_relative_to(forbidden):
        raise RuntimeError("smoke imported the source tree instead of the installed wheel")
    desktop_path = Path(desktop_client.__file__).resolve()
    if desktop_path.is_relative_to(forbidden):
        raise RuntimeError("desktop smoke imported the source tree instead of the installed wheel")
    if "app" in vars(main_module):
        raise RuntimeError("app.main constructed a global ASGI application during import")

    default_resource = resources.files("app.resources").joinpath("default_config.yaml")
    default_config = default_resource.read_text(encoding="utf-8")
    if "schema_version: 1" not in default_config or "playback_mode: silent" not in default_config:
        raise RuntimeError("packaged default configuration is missing or unexpected")

    console_scripts = {item.name for item in metadata.entry_points(group="console_scripts")}
    gui_scripts = {item.name for item in metadata.entry_points(group="gui_scripts")}
    if "megumin-companion-api" not in console_scripts:
        raise RuntimeError("console entry point is missing")
    if "megumin-companion-desktop" not in gui_scripts:
        raise RuntimeError("desktop entry point is missing")

    api_help, _ = _run_entry_point("megumin-companion-api", "--help")
    if "--check-config" not in api_help or "--dev-api" not in api_help:
        raise RuntimeError("installed API help is incomplete")
    api_version, _ = _run_entry_point("megumin-companion-api", "--version")
    if metadata.version("megumin-companion-ai") not in api_version:
        raise RuntimeError("installed API version output is unexpected")
    config_output, _ = _run_entry_point("megumin-companion-api", "--check-config")
    config_payload = json.loads(config_output)
    if config_payload.get("status") != "ok":
        raise RuntimeError("installed API configuration check failed")
    desktop_version, _ = _run_entry_point("megumin-companion-desktop", "--version")
    if metadata.version("megumin-companion-ai") not in desktop_version:
        raise RuntimeError("installed desktop version output is unexpected")
    _, desktop_error = _run_entry_point("megumin-companion-desktop", expected_return=3)
    if "desktop_unavailable" not in desktop_error:
        raise RuntimeError("installed desktop preflight output is unexpected")

    locked_status, health_status, health_state = asyncio.run(
        _health_payload(main_module, DevAPIConfig, httpx)
    )
    if locked_status != 503:
        raise RuntimeError("installed ASGI factory was not locked without --dev-api credentials")
    if health_status != 200 or health_state != "ready":
        raise RuntimeError("installed ASGI health smoke failed")
    after_cwd = _snapshot_directory(current_directory)
    if after_cwd != before_cwd:
        raise RuntimeError("installed runtime wrote to the arbitrary current working directory")

    print(
        json.dumps(
            {
                "status": "ok",
                "version": metadata.version("megumin-companion-ai"),
                "resource_bytes": len(default_config.encode("utf-8")),
                "locked_health_status": locked_status,
                "authenticated_health_status": health_status,
                "authenticated_health_state": health_state,
                "api_help": True,
                "api_check_config": True,
                "desktop_preflight": True,
                "cwd_unchanged": True,
                "source_tree_imported": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
