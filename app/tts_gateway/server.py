"""Minimal authenticated loopback ASGI surface for private TTS inference."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from app.tts_gateway.contracts import GatewayTTSRequest
from app.tts_gateway.engine import GatewayEngine, GatewayEngineError, GatewayEngineErrorCode

_MAX_JSON_BYTES = 32 * 1024
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_NO_STORE = {"Cache-Control": "no-store"}
ClientValidator = Callable[[str], bool]


def create_gateway_app(
    engine: GatewayEngine,
    bearer_token: str,
    *,
    expected_host: str = "127.0.0.1:9880",
    client_validator: ClientValidator | None = None,
) -> FastAPI:
    """Create exactly two authenticated routes with a three-request admission cap."""

    if not _TOKEN.fullmatch(bearer_token):
        raise ValueError("gateway bearer token must contain 256 bits")
    validator = client_validator or _is_loopback
    application = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    admission_lock = asyncio.Lock()
    admitted = 0

    @application.middleware("http")
    async def security_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        nonlocal admitted
        raw_headers = request.scope.get("headers", ())
        host_values = [value for name, value in raw_headers if name.lower() == b"host"]
        auth_values = [value for name, value in raw_headers if name.lower() == b"authorization"]
        origin_values = [value for name, value in raw_headers if name.lower() == b"origin"]
        client = request.client
        if (
            client is None
            or not validator(client.host)
            or host_values != [expected_host.encode("ascii")]
            or origin_values
            or len(auth_values) != 1
            or not _authorized(auth_values[0], bearer_token)
        ):
            return _error(403, "tts_gateway_forbidden")
        async with admission_lock:
            if admitted >= 3:
                return _error(429, "tts_gateway_busy")
            admitted += 1
        try:
            response = await call_next(request)
        except GatewayEngineError as exc:
            response = _engine_error(exc)
        except Exception:
            response = _error(500, "tts_gateway_failed")
        finally:
            async with admission_lock:
                admitted -= 1
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-TTS-Gateway-Protocol"] = "1"
        return response

    @application.get("/v1/health")
    async def health() -> Response:
        snapshot = engine.health()
        return JSONResponse(
            status_code=200 if snapshot.status == "ready" else 503,
            content=snapshot.model_dump(mode="json"),
            headers=_NO_STORE,
        )

    @application.post("/v1/tts")
    async def synthesize(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        content_length = request.headers.get("content-length")
        if content_type != "application/json" or content_length is None:
            return _error(415, "tts_gateway_invalid_request")
        try:
            declared = int(content_length)
        except ValueError:
            return _error(400, "tts_gateway_invalid_request")
        if declared < 1 or declared > _MAX_JSON_BYTES:
            return _error(413, "tts_gateway_request_too_large")
        body = await request.body()
        if len(body) != declared or len(body) > _MAX_JSON_BYTES:
            return _error(413, "tts_gateway_request_too_large")
        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_constant,
            )
            selected = GatewayTTSRequest.model_validate(payload)
        except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError):
            return _error(422, "tts_gateway_invalid_request")
        audio = await engine.synthesize(
            selected.voice_slot,
            selected.text,
            speed_factor=selected.speed_factor,
        )
        return Response(
            content=audio,
            media_type="audio/wav",
            headers=_NO_STORE,
        )

    return application


def _authorized(raw: bytes, expected: str) -> bool:
    try:
        value = raw.decode("ascii")
    except UnicodeError:
        return False
    prefix = "Bearer "
    if not value.startswith(prefix):
        return False
    supplied = value[len(prefix) :]
    return _TOKEN.fullmatch(supplied) is not None and hmac.compare_digest(
        supplied,
        expected,
    )


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.casefold() == "localhost"


def _engine_error(error: GatewayEngineError) -> JSONResponse:
    status = 503
    if error.code is GatewayEngineErrorCode.slot_invalid:
        status = 422
    elif error.code in {
        GatewayEngineErrorCode.inference_failed,
        GatewayEngineErrorCode.audio_invalid,
    }:
        status = 500
    return _error(status, error.code.value)


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error_code": code},
        headers=_NO_STORE,
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")
