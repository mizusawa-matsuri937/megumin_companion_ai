from __future__ import annotations

import asyncio
import io
import wave
from typing import Any, cast

import httpx
import pytest
from app.tts_gateway.contracts import GatewayHealth
from app.tts_gateway.engine import GatewayEngine, GatewayEngineError, GatewayEngineErrorCode
from app.tts_gateway.server import (
    _authorized,
    _is_loopback,
    create_gateway_app,
)
from fastapi.testclient import TestClient

_TOKEN = "A" * 43
_BASE = "http://127.0.0.1:9880"


def _wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(32_000)
        target.writeframes(b"\x00\x00" * 320)
    return output.getvalue()


class _Engine:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, float]] = []
        self.release = asyncio.Event()
        self.block = False
        self.health_status = "ready"
        self.error: Exception | None = None

    def health(self) -> GatewayHealth:
        return GatewayHealth(
            status=cast(Any, self.health_status),
            current_slot="neutral" if self.health_status == "ready" else None,
        )

    async def synthesize(self, slot: str, text: str, *, speed_factor: float) -> bytes:
        self.requests.append((slot, text, speed_factor))
        if self.error is not None:
            raise self.error
        if self.block:
            await self.release.wait()
        return _wav()


def _headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}", **extra}


def test_gateway_exposes_only_authenticated_strict_routes() -> None:
    engine = _Engine()
    application = create_gateway_app(cast(GatewayEngine, engine), _TOKEN)
    with TestClient(
        application,
        base_url=_BASE,
        client=("127.0.0.1", 53000),
    ) as client:
        health = client.get("/v1/health", headers=_headers())
        assert health.status_code == 200
        assert health.json() == {
            "protocol_version": 1,
            "status": "ready",
            "current_slot": "neutral",
        }
        assert health.headers["x-tts-gateway-protocol"] == "1"
        assert health.headers["cache-control"] == "no-store"

        response = client.post(
            "/v1/tts",
            headers=_headers(),
            json={"text": "中文测试", "voice_slot": "gentle", "speed_factor": 1.05},
        )
        assert response.status_code == 200
        assert response.content[:4] == b"RIFF"
        assert engine.requests == [("gentle", "中文测试", 1.05)]

        assert client.get("/docs", headers=_headers()).status_code == 404
        assert client.get("/v1/health/", headers=_headers()).status_code == 404
        assert client.put("/v1/tts", headers=_headers()).status_code == 405


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer " + "B" * 43},
        _headers(Origin="http://127.0.0.1:9880"),
        _headers(Host="localhost:9880"),
    ],
)
def test_gateway_rejects_missing_auth_origin_and_abnormal_host(
    headers: dict[str, str],
) -> None:
    application = create_gateway_app(cast(GatewayEngine, _Engine()), _TOKEN)
    with TestClient(application, base_url=_BASE, client=("127.0.0.1", 53001)) as client:
        response = client.get("/v1/health", headers=headers)
    assert response.status_code == 403
    assert response.json() == {"error_code": "tts_gateway_forbidden"}


@pytest.mark.parametrize(
    "body",
    [
        b'{"text":"x","voice_slot":"neutral","speed_factor":1,"extra":1}',
        b'{"text":"x","voice_slot":"unknown","speed_factor":1}',
        b'{"text":"x","voice_slot":"neutral","speed_factor":NaN}',
        b'{"text":"x","text":"y","voice_slot":"neutral","speed_factor":1}',
    ],
)
def test_gateway_rejects_invalid_or_duplicated_json(body: bytes) -> None:
    application = create_gateway_app(cast(GatewayEngine, _Engine()), _TOKEN)
    with TestClient(application, base_url=_BASE, client=("127.0.0.1", 53002)) as client:
        response = client.post(
            "/v1/tts",
            headers={**_headers(), "Content-Type": "application/json"},
            content=body,
        )
    assert response.status_code == 422
    assert response.json() == {"error_code": "tts_gateway_invalid_request"}


def test_gateway_admits_one_active_and_only_two_waiting_requests() -> None:
    async def scenario() -> None:
        engine = _Engine()
        engine.block = True
        application = create_gateway_app(cast(GatewayEngine, engine), _TOKEN)
        transport = httpx.ASGITransport(
            app=application,
            client=("127.0.0.1", 53003),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_BASE,
            headers=_headers(),
        ) as client:
            payload = {"text": "等待", "voice_slot": "neutral", "speed_factor": 1.0}
            admitted = [asyncio.create_task(client.post("/v1/tts", json=payload)) for _ in range(3)]
            while len(engine.requests) < 3:
                await asyncio.sleep(0)
            rejected = await client.post("/v1/tts", json=payload)
            assert rejected.status_code == 429
            assert rejected.json() == {"error_code": "tts_gateway_busy"}
            engine.release.set()
            completed = await asyncio.gather(*admitted)
        assert [response.status_code for response in completed] == [200, 200, 200]

    asyncio.run(scenario())


def test_gateway_rejects_invalid_token_client_and_request_framing() -> None:
    with pytest.raises(ValueError):
        create_gateway_app(cast(GatewayEngine, _Engine()), "short")

    denied = create_gateway_app(
        cast(GatewayEngine, _Engine()),
        _TOKEN,
        client_validator=lambda _host: False,
    )
    with TestClient(denied, base_url=_BASE, client=("127.0.0.1", 53004)) as client:
        assert client.get("/v1/health", headers=_headers()).status_code == 403

    application = create_gateway_app(cast(GatewayEngine, _Engine()), _TOKEN)
    with TestClient(application, base_url=_BASE, client=("127.0.0.1", 53005)) as client:
        wrong_type = client.post(
            "/v1/tts",
            headers={**_headers(), "Content-Type": "text/plain"},
            content=b"{}",
        )
        assert wrong_type.status_code == 415

        invalid_length = client.post(
            "/v1/tts",
            headers={
                **_headers(),
                "Content-Type": "application/json",
                "Content-Length": "invalid",
            },
            content=b"{}",
        )
        assert invalid_length.status_code == 400

        empty = client.post(
            "/v1/tts",
            headers={
                **_headers(),
                "Content-Type": "application/json",
                "Content-Length": "0",
            },
            content=b"",
        )
        assert empty.status_code == 413

        oversized = client.post(
            "/v1/tts",
            headers={
                **_headers(),
                "Content-Type": "application/json",
                "Content-Length": str(32 * 1024 + 1),
            },
            content=b"{}",
        )
        assert oversized.status_code == 413

        mismatched = client.post(
            "/v1/tts",
            headers={
                **_headers(),
                "Content-Type": "application/json",
                "Content-Length": "10",
            },
            content=b"{}",
        )
        assert mismatched.status_code == 413

        invalid_utf8 = client.post(
            "/v1/tts",
            headers={
                **_headers(),
                "Content-Type": "application/json",
                "Content-Length": "1",
            },
            content=b"\xff",
        )
        assert invalid_utf8.status_code == 422


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    (
        (
            GatewayEngineError(GatewayEngineErrorCode.slot_invalid),
            422,
            "tts_gateway_slot_invalid",
        ),
        (
            GatewayEngineError(GatewayEngineErrorCode.inference_failed),
            500,
            "tts_gateway_inference_failed",
        ),
        (
            GatewayEngineError(GatewayEngineErrorCode.audio_invalid),
            500,
            "tts_gateway_audio_invalid",
        ),
        (
            GatewayEngineError(GatewayEngineErrorCode.quarantine),
            503,
            "tts_gateway_quarantine",
        ),
        (RuntimeError("private engine failure"), 500, "tts_gateway_failed"),
    ),
)
def test_gateway_maps_engine_failures_without_detail(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    engine = _Engine()
    engine.error = error
    application = create_gateway_app(cast(GatewayEngine, engine), _TOKEN)
    with TestClient(application, base_url=_BASE, client=("127.0.0.1", 53006)) as client:
        response = client.post(
            "/v1/tts",
            headers=_headers(),
            json={"text": "正文", "voice_slot": "neutral", "speed_factor": 1.0},
        )
    assert response.status_code == expected_status
    assert response.json() == {"error_code": expected_code}
    assert "private engine failure" not in response.text


def test_gateway_quarantine_health_and_auth_helpers() -> None:
    engine = _Engine()
    engine.health_status = "quarantine"
    application = create_gateway_app(cast(GatewayEngine, engine), _TOKEN)
    with TestClient(application, base_url=_BASE, client=("127.0.0.1", 53007)) as client:
        response = client.get("/v1/health", headers=_headers())
    assert response.status_code == 503
    assert response.json()["status"] == "quarantine"

    assert _authorized(f"Bearer {_TOKEN}".encode("ascii"), _TOKEN)
    assert not _authorized(b"\xff", _TOKEN)
    assert not _authorized(b"Basic token", _TOKEN)
    assert not _authorized(f"Bearer {'B' * 42}".encode("ascii"), _TOKEN)
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("localhost")
    assert not _is_loopback("not-an-address")
