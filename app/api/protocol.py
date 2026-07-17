"""Versioned, bounded development API message helpers."""

from __future__ import annotations

import json
import math
import secrets
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.security import DEV_API_PROTOCOL_VERSION, DevAPIConfig, DevAPIPrincipal
from app.schemas import PipelineEvent, UserMessage


class DevAPIProtocolError(RuntimeError):
    """Stable protocol error that never carries the rejected input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WebSocketCommandEnvelope(BaseModel):
    """Protocol-v1 client command envelope."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    session_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]


def _reject_nonstandard_number(_value: str) -> None:
    raise ValueError("non-standard JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_websocket_command(text: str) -> WebSocketCommandEnvelope:
    """Parse a bounded frame without reflecting validation input in an error."""

    try:
        payload = json.loads(
            text,
            parse_constant=_reject_nonstandard_number,
            object_pairs_hook=_unique_object,
        )
        return WebSocketCommandEnvelope.model_validate(payload)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, ValidationError) as exc:
        raise DevAPIProtocolError("invalid_command_envelope") from exc


def ensure_authorized_session(principal: DevAPIPrincipal, session_id: str) -> None:
    if not secrets.compare_digest(
        principal.session_id.encode("utf-8"),
        session_id.encode("utf-8"),
    ):
        raise DevAPIProtocolError("session_forbidden")


def validate_metadata(metadata: dict[str, Any], config: DevAPIConfig) -> None:
    """Bound aggregate metadata depth, keys, and nodes after JSON decoding."""

    total_keys = 0
    total_nodes = 0
    pending: list[tuple[Any, int]] = [(metadata, 1)]
    while pending:
        value, depth = pending.pop()
        total_nodes += 1
        if total_nodes > config.max_metadata_nodes or depth > config.max_metadata_depth:
            raise DevAPIProtocolError("metadata_limits_exceeded")
        if isinstance(value, dict):
            total_keys += len(value)
            if total_keys > config.max_metadata_keys:
                raise DevAPIProtocolError("metadata_limits_exceeded")
            for key, item in value.items():
                if not isinstance(key, str) or not 1 <= len(key) <= 128:
                    raise DevAPIProtocolError("metadata_limits_exceeded")
                pending.append((item, depth + 1))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif (isinstance(value, float) and not math.isfinite(value)) or (
            value is not None and not isinstance(value, (str, int, float, bool))
        ):
            raise DevAPIProtocolError("metadata_limits_exceeded")


def validate_user_message(
    message: UserMessage,
    principal: DevAPIPrincipal,
    config: DevAPIConfig,
    *,
    now: datetime | None = None,
) -> None:
    ensure_authorized_session(principal, message.session_id)
    if message.created_at.tzinfo is None or message.created_at.utcoffset() is None:
        raise DevAPIProtocolError("client_time_out_of_range")
    reference = now or datetime.now(UTC)
    skew = abs((message.created_at.astimezone(UTC) - reference.astimezone(UTC)).total_seconds())
    if skew > config.max_client_clock_skew_seconds:
        raise DevAPIProtocolError("client_time_out_of_range")
    validate_metadata(message.metadata, config)


def event_envelope(event: PipelineEvent) -> dict[str, Any]:
    return {
        "protocol_version": DEV_API_PROTOCOL_VERSION,
        **event.model_dump(mode="json"),
    }


def error_envelope(*, session_id: str, code: str) -> dict[str, Any]:
    return {
        "protocol_version": DEV_API_PROTOCOL_VERSION,
        "type": "error",
        "session_id": session_id,
        "error": {"code": code},
    }
