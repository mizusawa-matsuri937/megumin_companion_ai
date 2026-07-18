"""Property and fuzz coverage for the W12 helper frame parser."""

from __future__ import annotations

import json
import os
import struct

import pytest
from app.workers.protocol import (
    MAX_HELPER_BUFFER_BYTES,
    MAX_HELPER_FRAME_BYTES,
    FrameDecoder,
    HelperMessage,
    ProtocolError,
    decode_payload,
    encode_message,
)
from hypothesis import given
from hypothesis import strategies as st


@given(
    st.text(
        alphabet=st.characters(
            codec="utf-8",
            blacklist_characters="\x00",
        ),
        max_size=256,
    ),
    st.lists(st.integers(min_value=-(2**20), max_value=2**20), max_size=8),
    st.lists(st.integers(min_value=1, max_value=31), min_size=1, max_size=16),
)
def test_round_trip_across_arbitrary_pipe_chunk_boundaries(
    text: str,
    values: list[int],
    chunk_sizes: list[int],
) -> None:
    message = HelperMessage(
        message_type="job.completed",
        request_id="job_1",
        payload={"text_value": text, "values": values},
    )
    encoded = encode_message(message)
    decoder = FrameDecoder()
    decoded: list[HelperMessage] = []
    offset = 0
    for size in chunk_sizes:
        decoded.extend(decoder.feed(encoded[offset : offset + size]))
        offset += size
        if offset >= len(encoded):
            break
    decoded.extend(decoder.feed(encoded[offset:]))
    decoder.finish()
    assert decoded == [message]
    assert decoder.buffered_bytes == 0


@given(st.binary(min_size=0, max_size=2048))
def test_random_bytes_never_escape_protocol_error_or_remain_unbounded(data: bytes) -> None:
    decoder = FrameDecoder()
    try:
        decoder.feed(data)
        decoder.finish()
    except ProtocolError as exc:
        assert exc.code.startswith("helper_protocol_")
    assert decoder.buffered_bytes <= MAX_HELPER_BUFFER_BYTES


@pytest.mark.parametrize(
    "payload,code",
    [
        (b"", "helper_protocol_frame_size_invalid"),
        (b"{", "helper_protocol_json_invalid"),
        (b"\xff", "helper_protocol_json_invalid"),
        (
            b'{"schema_version":1,"schema_version":1,"message_type":"hello","request_id":null,"payload":{}}',
            "helper_protocol_duplicate_key",
        ),
        (
            b'{"schema_version":2,"message_type":"hello","request_id":null,"payload":{}}',
            "helper_protocol_version_mismatch",
        ),
        (
            b'{"schema_version":1,"message_type":"hello","request_id":null,"payload":{"value":NaN}}',
            "helper_protocol_non_finite_number",
        ),
    ],
)
def test_malformed_payloads_fail_with_content_free_codes(payload: bytes, code: str) -> None:
    decoder = FrameDecoder()
    frame = struct.pack(">I", len(payload)) + payload
    with pytest.raises(ProtocolError) as raised:
        decoder.feed(frame)
    assert raised.value.code == code
    decoded_payload = payload.decode("utf-8", errors="ignore")
    if decoded_payload:
        assert decoded_payload not in str(raised.value)


def test_oversized_declared_and_streamed_frames_fail_before_unbounded_buffering() -> None:
    decoder = FrameDecoder()
    with pytest.raises(ProtocolError, match="frame_size_invalid"):
        decoder.feed(struct.pack(">I", MAX_HELPER_FRAME_BYTES + 1))
    decoder = FrameDecoder()
    with pytest.raises(ProtocolError, match="buffer_overflow"):
        decoder.feed(b"\x00\x01\x00\x00" + os.urandom(MAX_HELPER_FRAME_BYTES + 1))
    assert decoder.buffered_bytes <= MAX_HELPER_BUFFER_BYTES


def test_truncated_header_and_payload_are_rejected_at_eof() -> None:
    for partial in (b"\x00", struct.pack(">I", 100) + b"{}"):
        decoder = FrameDecoder()
        assert decoder.feed(partial) == ()
        with pytest.raises(ProtocolError, match="truncated_frame"):
            decoder.finish()


def test_schema_forbids_unknown_fields_deep_payload_and_pickle_like_objects() -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "message_type": "hello",
            "request_id": None,
            "payload": {},
            "pickle": "forbidden",
        }
    ).encode()
    with pytest.raises(ProtocolError, match="envelope_invalid"):
        FrameDecoder().feed(struct.pack(">I", len(raw)) + raw)
    with pytest.raises(ProtocolError, match="too_deep"):
        HelperMessage(message_type="hello", payload={"a": {"b": {"c": {"d": 1}}}})
    with pytest.raises(ProtocolError, match="value_type_invalid"):
        HelperMessage(message_type="hello", payload={"object": object()})


def test_multiple_frames_decode_without_cross_frame_confusion() -> None:
    first = HelperMessage(message_type="heartbeat", payload={"sequence": 1})
    second = HelperMessage(message_type="heartbeat", payload={"sequence": 2})
    decoder = FrameDecoder()
    assert decoder.feed(encode_message(first) + encode_message(second)) == (first, second)
    decoder.finish()


@pytest.mark.parametrize(
    "message",
    (
        lambda: HelperMessage(schema_version=2, message_type="hello", payload={}),
        lambda: HelperMessage(message_type="INVALID", payload={}),
        lambda: HelperMessage(message_type="hello", request_id="bad id", payload={}),
        lambda: HelperMessage(message_type="hello", payload={"INVALID": 1}),
        lambda: HelperMessage(message_type="hello", payload={"value": 2**54}),
        lambda: HelperMessage(message_type="hello", payload={"value": "x\x00y"}),
        lambda: HelperMessage(message_type="hello", payload={"values": [0] * 17}),
    ),
)
def test_message_model_rejects_version_identity_and_payload_bounds(message: object) -> None:
    with pytest.raises(ProtocolError):
        message()  # type: ignore[operator]


def test_decoder_requires_bytes_and_encoder_enforces_utf8_frame_limit() -> None:
    with pytest.raises(ProtocolError, match="bytes_required"):
        FrameDecoder().feed(bytearray(b"x"))  # type: ignore[arg-type]
    with pytest.raises(ProtocolError, match="frame_too_large"):
        encode_message(
            HelperMessage(
                message_type="hello",
                payload={f"value_{index}": "界" * 4096 for index in range(16)},
            )
        )


def test_direct_payload_and_envelope_type_checks_cover_fail_closed_edges() -> None:
    assert HelperMessage(message_type="hello", payload={"value": 1.5}).payload == {"value": 1.5}
    with pytest.raises(ProtocolError, match="non_finite"):
        HelperMessage(message_type="hello", payload={"value": float("inf")})
    with pytest.raises(ProtocolError, match="collection_too_large"):
        HelperMessage(
            message_type="hello",
            payload={f"key_{index}": index for index in range(17)},
        )
    with pytest.raises(ProtocolError, match="payload_invalid"):
        HelperMessage(message_type="hello", payload=[])  # type: ignore[arg-type]
    with pytest.raises(ProtocolError, match="frame_size_invalid"):
        decode_payload(b"")

    base = {
        "schema_version": 1,
        "message_type": "hello",
        "request_id": None,
        "payload": {},
    }
    for field, value, code in (
        ("schema_version", True, "version_invalid"),
        ("message_type", 1, "message_type_invalid"),
        ("request_id", 1, "request_id_invalid"),
        ("payload", [], "payload_invalid"),
    ):
        envelope = {**base, field: value}
        with pytest.raises(ProtocolError, match=code):
            decode_payload(json.dumps(envelope).encode("utf-8"))
