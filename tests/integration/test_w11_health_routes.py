"""W11 authenticated health route separation and response allowlist tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from app.api.security import DevAPIConfig, DevAPIScope
from app.config import Settings
from app.config.settings import LoggingConfig
from app.health import CapabilityCheck, CapabilityState
from app.main import create_app
from app.paths import AppPaths
from fastapi.testclient import TestClient

BASE_URL = "http://127.0.0.1:8765"
DEV_API = DevAPIConfig(
    token="w11-health-token-000000000000000000000000",
    session_id="session_w11_health",
    allowed_origins=frozenset({BASE_URL}),
    allowed_hosts=frozenset({"127.0.0.1:8765"}),
    scopes=frozenset({DevAPIScope.chat}),
)


@dataclass
class _Provider:
    name: str
    required_for_readiness: bool
    result: CapabilityCheck

    async def check_health(self) -> CapabilityCheck:
        return self.result


def _settings(root: Path) -> Settings:
    settings = Settings(logging=LoggingConfig(console_enabled=False, file_enabled=False))
    settings._paths = AppPaths(root=root)
    return settings


def _client(application: object) -> TestClient:
    return TestClient(
        application,  # type: ignore[arg-type]
        base_url=BASE_URL,
        headers=DEV_API.client_headers(),
        client=("127.0.0.1", 51111),
    )


def test_health_routes_separate_liveness_readiness_and_capabilities(tmp_path: Path) -> None:
    optional = _Provider(
        "vts",
        False,
        CapabilityCheck(status=CapabilityState.degraded, error_code="vts_disconnected"),
    )
    application = create_app(
        _settings(tmp_path / "app"),
        dev_api=DEV_API,
        health_providers=(optional,),
    )

    with _client(application) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        compatibility = client.get("/health")
        capabilities = client.get("/health/capabilities")

    assert live.status_code == 200 and live.json()["status"] == "live"
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assert compatibility.json() == ready.json()
    assert capabilities.status_code == 200
    assert capabilities.json()["capabilities"] == [
        {"name": "core", "status": "ready"},
        {"name": "vts", "status": "degraded", "error_code": "vts_disconnected"},
    ]


def test_required_capability_failure_returns_503_without_details(tmp_path: Path) -> None:
    required = _Provider(
        "provider",
        True,
        CapabilityCheck(status=CapabilityState.unavailable, error_code="provider_timeout"),
    )
    application = create_app(
        _settings(tmp_path / "app"),
        dev_api=DEV_API,
        health_providers=(required,),
    )

    with _client(application) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == [
        {"name": "core", "status": "ready"},
        {"name": "provider", "status": "unavailable", "error_code": "provider_timeout"},
    ]
    serialized = response.text
    assert "path" not in serialized
    assert "message" not in serialized
    assert "detail" not in serialized


def test_health_response_field_allowlist_is_exact(tmp_path: Path) -> None:
    application = create_app(_settings(tmp_path / "app"), dev_api=DEV_API)

    with _client(application) as client:
        payloads = {
            "live": client.get("/health/live").json(),
            "ready": client.get("/health/ready").json(),
            "capabilities": client.get("/health/capabilities").json(),
        }

    common = {"schema_version", "status", "service", "version"}
    assert set(payloads["live"]) == common
    assert set(payloads["ready"]) == common | {"checks"}
    assert set(payloads["capabilities"]) == common | {"capabilities"}
    for field in (*payloads["ready"]["checks"], *payloads["capabilities"]["capabilities"]):
        assert set(field) <= {"name", "status", "error_code"}


def test_startup_failure_writes_content_free_crash_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "app"

    def fail_pipeline(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("W11_CRASH_ROUTE_SENTINEL C:\\Users\\private-user\\draft.txt")

    monkeypatch.setattr("app.main.build_dialogue_pipeline", fail_pipeline)
    application = create_app(_settings(root), dev_api=DEV_API)

    with pytest.raises(RuntimeError, match="W11_CRASH_ROUTE_SENTINEL"), _client(application):
        pass

    reports = list((root / "logs").glob("crash.*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["error_code"] == "application_runtime_failure"
    assert "W11_CRASH_ROUTE_SENTINEL" not in json.dumps(payload)
    assert "private-user" not in json.dumps(payload)
