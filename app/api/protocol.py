"""Versioned, bounded development API message helpers."""

from __future__ import annotations

import json
import math
import secrets
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.security import DEV_API_PROTOCOL_VERSION, DevAPIConfig, DevAPIPrincipal
from app.schemas import PipelineEvent, SessionReset, SessionSnapshotChunk, UserMessage


class DevAPIProtocolError(RuntimeError):
    """Stable protocol error that never carries the rejected input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _WebSocketCommandWire(BaseModel):
    """Client-controlled portion of the finalized protocol-v1 command."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    command_id: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    session_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]


class _LegacyWebSocketCommandWire(BaseModel):
    """One-version W04 development-client compatibility shape."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    session_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]


class CommandEnvelope(BaseModel):
    """Validated command with the backend-owned receipt timestamp."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    command_id: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    session_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]
    received_at: datetime
    legacy_protocol: bool = False


def _reject_nonstandard_number(_value: str) -> None:
    raise ValueError("non-standard JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_websocket_command(
    text: str,
    *,
    now: datetime | None = None,
    legacy_client_id: str | None = None,
) -> CommandEnvelope:
    """Parse a bounded frame without reflecting validation input in an error."""

    try:
        payload = json.loads(
            text,
            parse_constant=_reject_nonstandard_number,
            object_pairs_hook=_unique_object,
        )
        received_at = now or datetime.now(UTC)
        try:
            wire = _WebSocketCommandWire.model_validate(payload)
            return CommandEnvelope(**wire.model_dump(), received_at=received_at)
        except ValidationError:
            if legacy_client_id is None or not 1 <= len(legacy_client_id) <= 128:
                raise
            legacy = _LegacyWebSocketCommandWire.model_validate(payload)
            return CommandEnvelope(
                protocol_version=legacy.protocol_version,
                command_id=f"legacy_{uuid4().hex}",
                client_id=legacy_client_id,
                type=legacy.type,
                session_id=legacy.session_id,
                payload=legacy.payload,
                received_at=received_at,
                legacy_protocol=True,
            )
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, ValidationError) as exc:
        raise DevAPIProtocolError("invalid_command_envelope") from exc


def ensure_authorized_session(principal: DevAPIPrincipal, session_id: str) -> None:
    if not secrets.compare_digest(
        principal.session_id.encode("utf-8"),
        session_id.encode("utf-8"),
    ):
        raise DevAPIProtocolError("session_forbidden")


def ensure_authorized_identity(
    principal: DevAPIPrincipal,
    *,
    client_id: str,
    session_id: str,
) -> None:
    if not secrets.compare_digest(
        principal.client_id.encode("utf-8"),
        client_id.encode("utf-8"),
    ):
        raise DevAPIProtocolError("client_identity_forbidden")
    ensure_authorized_session(principal, session_id)


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


def reset_envelope(reset: SessionReset) -> dict[str, Any]:
    return {
        "protocol_version": DEV_API_PROTOCOL_VERSION,
        **reset.model_dump(mode="json"),
    }


def snapshot_chunk_envelope(chunk: SessionSnapshotChunk) -> dict[str, Any]:
    return {
        "protocol_version": DEV_API_PROTOCOL_VERSION,
        **chunk.model_dump(mode="json"),
    }


def error_envelope(*, session_id: str, code: str) -> dict[str, Any]:
    return {
        "protocol_version": DEV_API_PROTOCOL_VERSION,
        "type": "error",
        "session_id": session_id,
        "error": {"code": code},
    }
