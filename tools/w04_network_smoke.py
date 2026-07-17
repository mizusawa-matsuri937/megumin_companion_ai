"""Exercise the W04 boundary through a real loopback Uvicorn socket."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect
from websockets.typing import Origin

HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 20.0


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((HOST, 0))
        return int(listener.getsockname()[1])


def _port_accepts_connections(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.settimeout(0.2)
        return candidate.connect_ex((HOST, port)) == 0


def _wait_for_health(
    client: httpx.Client,
    headers: dict[str, str],
    process: subprocess.Popen[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last_error = "not_started"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"development API exited during startup: {process.returncode}")
        try:
            response = client.get("/health", headers=headers)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
            last_error = f"http_{response.status_code}"
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
        time.sleep(0.05)
    raise RuntimeError(f"development API did not become ready: {last_error}")


def _websocket_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in {"origin", "host"}}


def _verify_websocket(
    *,
    port: int,
    headers: dict[str, str],
    session_id: str,
) -> None:
    uri = f"ws://{HOST}:{port}/ws/client"
    websocket_headers = _websocket_headers(headers)
    with connect(
        uri,
        origin=Origin(headers["Origin"]),
        additional_headers=websocket_headers,
        max_size=64 * 1024,
        open_timeout=5,
    ) as websocket:
        websocket.send(
            json.dumps(
                {
                    "protocol_version": 1,
                    "type": "user.message",
                    "session_id": session_id,
                    "payload": {
                        "text": "W04 deterministic network smoke",
                        "session_id": session_id,
                    },
                },
                separators=(",", ":"),
            )
        )
        response = json.loads(websocket.recv())
        if response.get("protocol_version") != 1 or response.get("type") != "turn.accepted":
            raise RuntimeError("valid protocol-v1 WebSocket command was not accepted")
        while response.get("type") != "assistant.completed":
            response = json.loads(websocket.recv())

    try:
        with connect(
            uri,
            origin=Origin("http://attacker.example"),
            additional_headers=websocket_headers,
            open_timeout=5,
        ):
            raise RuntimeError("attacker WebSocket Origin was accepted")
    except InvalidStatus:
        pass

    try:
        with connect(
            uri,
            origin=Origin(headers["Origin"]),
            additional_headers=websocket_headers,
            open_timeout=5,
        ) as websocket:
            websocket.send("x" * (64 * 1024 + 1))
            websocket.recv()
            raise RuntimeError("oversized WebSocket frame was accepted")
    except ConnectionClosed as exc:
        if exc.code != 1009:
            raise RuntimeError(
                f"oversized WebSocket frame closed with {exc.code}, not 1009"
            ) from exc


def main() -> int:
    port = _unused_port()
    local_app_data = Path(tempfile.mkdtemp(prefix="megumin-w04-network-"))
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app",
            "--dev-api",
            "--host",
            HOST,
            "--port",
            str(port),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=creation_flags,
    )
    token = ""
    session_id = ""
    private_attack_value = "w04-private-attack-sentinel"
    remaining_stdout = ""
    stderr = ""
    try:
        assert process.stdout is not None
        credential_line = process.stdout.readline()
        credential = json.loads(credential_line)
        token = str(credential["token"])
        session_id = str(credential["session_id"])
        origin = str(credential["allowed_origins"][0])
        if credential.get("scopes") != ["chat"]:
            raise RuntimeError("default development token was not least-privilege chat-only")
        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": origin,
            "X-Megumin-Protocol": "1",
            "X-Megumin-Session-ID": session_id,
        }
        base_url = f"http://{HOST}:{port}"
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            health = _wait_for_health(client, headers, process)
            if health.get("status") != "ready":
                raise RuntimeError("authenticated health did not report ready")
            if client.get("/debug/state", headers=headers).status_code != 403:
                raise RuntimeError("default development token unexpectedly has admin scope")

            wrong_token = dict(headers)
            wrong_token["Authorization"] = f"Bearer {private_attack_value}"
            rejected_token = client.get("/health", headers=wrong_token)
            if rejected_token.status_code != 401 or private_attack_value in rejected_token.text:
                raise RuntimeError("wrong HTTP token did not fail safely")

            attacker_origin = dict(headers)
            attacker_origin["Origin"] = "http://attacker.example"
            if client.get("/health", headers=attacker_origin).status_code != 403:
                raise RuntimeError("attacker HTTP Origin was accepted")

            oversized = client.post(
                "/api/chat",
                headers={**headers, "Content-Type": "application/json"},
                content=b"x" * (64 * 1024 + 1),
            )
            if oversized.status_code != 413:
                raise RuntimeError("oversized HTTP body was accepted")

        _verify_websocket(port=port, headers=headers, session_id=session_id)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            remaining_stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            remaining_stdout, stderr = process.communicate(timeout=10)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _port_accepts_connections(port):
            time.sleep(0.05)
        shutil.rmtree(local_app_data, ignore_errors=True)

    if _port_accepts_connections(port):
        raise RuntimeError("development API port remained open after process termination")
    if token and token in remaining_stdout + stderr:
        raise RuntimeError("runtime logs exposed the development API token")
    if session_id and session_id in remaining_stdout + stderr:
        raise RuntimeError("runtime logs exposed the development API session")
    if private_attack_value in remaining_stdout + stderr:
        raise RuntimeError("runtime logs exposed an attacker credential")
    print(
        json.dumps(
            {
                "status": "ok",
                "http_auth": "rejected",
                "http_admin_default": "rejected",
                "http_origin": "rejected",
                "http_oversized": "rejected",
                "websocket_origin": "rejected",
                "websocket_oversized": "closed_1009",
                "port_released": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
