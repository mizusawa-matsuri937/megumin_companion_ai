"""Versioned length-prefixed UTF-8 JSON helper protocol.

The protocol accepts data, never Python objects.  Every frame has a four-byte
big-endian length followed by one strict UTF-8 JSON object.  Unknown fields,
non-finite numbers, duplicate keys, and unbounded nesting fail closed.
"""

from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass
from typing import Any, Final

HELPER_PROTOCOL_VERSION: Final = 1
MAX_HELPER_FRAME_BYTES: Final = 64 * 1024
MAX_HELPER_BUFFER_BYTES: Final = MAX_HELPER_FRAME_BYTES + 4
MAX_PAYLOAD_KEYS: Final = 16
MAX_PAYLOAD_DEPTH: Final = 3
MAX_STRING_CHARS: Final = 4096
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_MESSAGE_FIELDS = frozenset({"schema_version", "message_type", "request_id", "payload"})


class ProtocolError(RuntimeError):
    """A stable protocol failure that never reflects attacker-controlled data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HelperMessage:
    message_type: str
    payload: dict[str, Any]
    request_id: str | None = None
    schema_version: int = HELPER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_message(self)


def _reject_constant(_value: str) -> None:
    raise ProtocolError("helper_protocol_non_finite_number")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("helper_protocol_duplicate_key")
        result[key] = value
    return result


def _validate_value(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise ProtocolError("helper_protocol_payload_too_deep")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > 2**53:
            raise ProtocolError("helper_protocol_integer_out_of_range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("helper_protocol_non_finite_number")
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS or "\x00" in value:
            raise ProtocolError("helper_protocol_string_invalid")
        return
    if isinstance(value, list):
        if len(value) > MAX_PAYLOAD_KEYS:
            raise ProtocolError("helper_protocol_collection_too_large")
        for item in value:
            _validate_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_PAYLOAD_KEYS:
            raise ProtocolError("helper_protocol_collection_too_large")
        for key, item in value.items():
            if not isinstance(key, str) or not _SAFE_CODE.fullmatch(key):
                raise ProtocolError("helper_protocol_payload_key_invalid")
            _validate_value(item, depth=depth + 1)
        return
    raise ProtocolError("helper_protocol_value_type_invalid")


def _validate_message(message: HelperMessage) -> None:
    if message.schema_version != HELPER_PROTOCOL_VERSION:
        raise ProtocolError("helper_protocol_version_mismatch")
    if not _SAFE_CODE.fullmatch(message.message_type):
        raise ProtocolError("helper_protocol_message_type_invalid")
    if message.request_id is not None and not _SAFE_ID.fullmatch(message.request_id):
        raise ProtocolError("helper_protocol_request_id_invalid")
    if not isinstance(message.payload, dict):
        raise ProtocolError("helper_protocol_payload_invalid")
    _validate_value(message.payload)


def encode_message(message: HelperMessage) -> bytes:
    """Serialize one validated message without pickle or object hooks."""

    _validate_message(message)
    raw = json.dumps(
        {
            "schema_version": message.schema_version,
            "message_type": message.message_type,
            "request_id": message.request_id,
            "payload": message.payload,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_HELPER_FRAME_BYTES:
        raise ProtocolError("helper_protocol_frame_too_large")
    return struct.pack(">I", len(raw)) + raw


def decode_payload(raw: bytes) -> HelperMessage:
    if not raw or len(raw) > MAX_HELPER_FRAME_BYTES:
        raise ProtocolError("helper_protocol_frame_size_invalid")
    try:
        text = raw.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_pairs,
        )
    except ProtocolError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("helper_protocol_json_invalid") from exc
    if not isinstance(decoded, dict) or set(decoded) != _MESSAGE_FIELDS:
        raise ProtocolError("helper_protocol_envelope_invalid")
    schema_version = decoded["schema_version"]
    message_type = decoded["message_type"]
    request_id = decoded["request_id"]
    payload = decoded["payload"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ProtocolError("helper_protocol_version_invalid")
    if not isinstance(message_type, str):
        raise ProtocolError("helper_protocol_message_type_invalid")
    if request_id is not None and not isinstance(request_id, str):
        raise ProtocolError("helper_protocol_request_id_invalid")
    if not isinstance(payload, dict):
        raise ProtocolError("helper_protocol_payload_invalid")
    return HelperMessage(
        schema_version=schema_version,
        message_type=message_type,
        request_id=request_id,
        payload=payload,
    )


class FrameDecoder:
    """Incremental bounded decoder for anonymous-pipe byte streams."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._expected: int | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> tuple[HelperMessage, ...]:
        if not isinstance(data, bytes):
            raise ProtocolError("helper_protocol_bytes_required")
        if len(self._buffer) + len(data) > MAX_HELPER_BUFFER_BYTES:
            raise ProtocolError("helper_protocol_buffer_overflow")
        self._buffer.extend(data)
        messages: list[HelperMessage] = []
        while True:
            if self._expected is None:
                if len(self._buffer) < 4:
                    break
                self._expected = struct.unpack(">I", self._buffer[:4])[0]
                del self._buffer[:4]
                if self._expected < 1 or self._expected > MAX_HELPER_FRAME_BYTES:
                    raise ProtocolError("helper_protocol_frame_size_invalid")
            if len(self._buffer) < self._expected:
                break
            raw = bytes(self._buffer[: self._expected])
            del self._buffer[: self._expected]
            self._expected = None
            messages.append(decode_payload(raw))
        return tuple(messages)

    def finish(self) -> None:
        if self._expected is not None or self._buffer:
            raise ProtocolError("helper_protocol_truncated_frame")
