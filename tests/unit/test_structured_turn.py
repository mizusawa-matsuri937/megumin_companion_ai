"""Strict incremental structured-output parsing and local plan resolution."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from app.avatar import FocusedVariant
from app.clients.llm.errors import LLMErrorCode, LLMProviderError
from app.emotion import EmotionEngine, EmotionLabel
from app.emotion.clock import FakeClock
from app.pipelines.structured_turn import (
    EmotionTurnPlanResolver,
    IncrementalTurnJSONParser,
    StructuredTurnPlan,
    TurnStreamFormat,
    _scan_compound,
    _scan_json_string,
    prepare_structured_turn_request,
    provider_turn_stream_format,
)
from app.schemas import ChatMessage, ChatRequest, ChatRole


def _payload(
    *,
    emotion: str = "focused",
    variant: str = "chuunibyou",
    red_eye: bool = True,
) -> str:
    return json.dumps(
        {
            "plan": {
                "emotion": emotion,
                "focused_variant": variant,
            },
            "segments": [
                {"text": "第一段中文。", "red_eye": red_eye},
                {"text": "第二段中文。", "red_eye": False},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 31, 10_000])
def test_parser_accepts_every_chunk_shape_and_releases_complete_segments(
    chunk_size: int,
) -> None:
    raw = _payload()
    parser = IncrementalTurnJSONParser(max_bytes=4096, max_segments=8)
    segments = []

    for start in range(0, len(raw), chunk_size):
        segments.extend(parser.feed(raw[start : start + chunk_size]))
    segments.extend(parser.finish())

    assert parser.plan == StructuredTurnPlan(
        emotion=EmotionLabel.focused,
        focused_variant=FocusedVariant.chuunibyou,
    )
    assert [segment.text for segment in segments] == ["第一段中文。", "第二段中文。"]
    assert [segment.red_eye for segment in segments] == [True, False]


def test_parser_holds_segments_until_plan_and_strips_only_outer_whitespace() -> None:
    parser = IncrementalTurnJSONParser(max_bytes=4096, max_segments=8)
    assert (
        parser.feed('{"segments":[{"text":"  日文名不应改写・中文正文。  ","red_eye":false}],')
        == []
    )
    released = parser.feed('"plan":{"emotion":"happy","focused_variant":"default"}}')

    assert [segment.text for segment in released] == ["日文名不应改写・中文正文。"]
    assert parser.finish() == []


@pytest.mark.parametrize(
    "raw",
    [
        '{"plan":{"emotion":"unknown","focused_variant":"default"},'
        '"segments":[{"text":"正文","red_eye":false}]}',
        '{"plan":{"emotion":"happy","focused_variant":"chuunibyou"},'
        '"segments":[{"text":"正文","red_eye":false}]}',
        '{"plan":{"emotion":"happy","focused_variant":"default","path":"x"},'
        '"segments":[{"text":"正文","red_eye":false}]}',
        '{"plan":{"emotion":"happy","focused_variant":"default"},'
        '"segments":[{"text":"正文","red_eye":false,"hotkey":"x"}]}',
        '{"plan":{"emotion":"happy","focused_variant":"default"},'
        '"segments":[{"text":"   ","red_eye":false}]}',
        '{"plan":{"emotion":"happy","focused_variant":"default"},'
        '"segments":[{"text":"正文","red_eye":"yes"}]}',
        '{"plan":{"emotion":"happy","focused_variant":"default"},"segments":[]}',
        '{"plan":{"emotion":"happy","focused_variant":"default"},'
        '"segments":[{"text":"正文","red_eye":false}],"unknown":1}',
        "```json\n" + _payload(emotion="happy", variant="default") + "\n```",
    ],
)
def test_parser_rejects_unknown_fields_enums_empty_content_and_wrappers(raw: str) -> None:
    parser = IncrementalTurnJSONParser(max_bytes=4096, max_segments=16)

    with pytest.raises(LLMProviderError) as caught:
        parser.feed(raw)
        parser.finish()

    assert caught.value.code is LLMErrorCode.structured
    assert not caught.value.retryable


def test_parser_rejects_truncation_duplicate_keys_limits_and_trailing_content() -> None:
    cases = [
        _payload()[:-1],
        '{"plan":{"emotion":"happy","focused_variant":"default"},'
        '"plan":{"emotion":"happy","focused_variant":"default"},'
        '"segments":[{"text":"正文","red_eye":false}]}',
        '{"plan":{"emotion":"happy","focused_variant":"default"},'
        '"segments":[{"text":"正文","red_eye":false}]} trailing',
        '{"plan":{"emotion":"happy","focused_variant":"default"},'
        '"segments":['
        + ",".join(f'{{"text":"第{index}段","red_eye":true}}' for index in range(9))
        + "]}",
    ]

    for raw in cases:
        parser = IncrementalTurnJSONParser(max_bytes=4096, max_segments=16)
        with pytest.raises(LLMProviderError) as caught:
            parser.feed(raw)
            parser.finish()
        assert caught.value.code is LLMErrorCode.structured

    too_small = IncrementalTurnJSONParser(
        max_bytes=8,
        max_segments=1,
        max_red_eye_segments=1,
    )
    with pytest.raises(LLMProviderError):
        too_small.feed('{"plan":{}')


def test_structured_request_inserts_a_bounded_system_instruction_before_user() -> None:
    request = ChatRequest(
        messages=[
            ChatMessage(role=ChatRole.system, content="原系统约束"),
            ChatMessage(role=ChatRole.user, content="你好"),
        ]
    )

    structured = prepare_structured_turn_request(request)

    assert structured.response_format == "json_object"
    assert [message.role for message in structured.messages] == [
        ChatRole.system,
        ChatRole.system,
        ChatRole.user,
    ]
    instruction = structured.messages[1].content
    assert isinstance(instruction, str)
    assert "prompt_lang" not in instruction
    assert "模型路径" in instruction

    full = ChatRequest(
        messages=[
            ChatMessage(role=ChatRole.user, content=f"message-{index}") for index in range(200)
        ]
    )
    with pytest.raises(LLMProviderError) as caught:
        prepare_structured_turn_request(full)
    assert caught.value.code is LLMErrorCode.structured


def test_local_resolver_applies_minimum_duration_and_explosion_cooldown() -> None:
    clock = FakeClock(datetime(2026, 7, 29, 12, tzinfo=UTC))
    engine = EmotionEngine(
        clock=clock,
        label_min_duration=timedelta(seconds=10),
        explosion_cooldown=timedelta(minutes=5),
    )
    resolver = EmotionTurnPlanResolver(engine)

    first = resolver(
        StructuredTurnPlan(
            emotion=EmotionLabel.focused,
            focused_variant=FocusedVariant.chuunibyou,
        )
    )
    assert first.emotion is EmotionLabel.focused
    assert first.focused_variant is FocusedVariant.chuunibyou

    suppressed = resolver(
        StructuredTurnPlan(
            emotion=EmotionLabel.happy,
            focused_variant=FocusedVariant.default,
        )
    )
    assert suppressed.emotion is EmotionLabel.focused
    assert suppressed.focused_variant is FocusedVariant.default

    clock.advance(timedelta(seconds=10))
    exploded = resolver(
        StructuredTurnPlan(
            emotion=EmotionLabel.explosion_mode,
            focused_variant=FocusedVariant.default,
        )
    )
    assert exploded.emotion is EmotionLabel.explosion_mode

    clock.advance(timedelta(seconds=10))
    assert (
        resolver(
            StructuredTurnPlan(
                emotion=EmotionLabel.neutral,
                focused_variant=FocusedVariant.default,
            )
        ).emotion
        is EmotionLabel.neutral
    )
    clock.advance(timedelta(seconds=10))
    assert (
        resolver(
            StructuredTurnPlan(
                emotion=EmotionLabel.explosion_mode,
                focused_variant=FocusedVariant.default,
            )
        ).emotion
        is EmotionLabel.neutral
    )


def test_disabled_resolver_ignores_llm_emotion_and_variant() -> None:
    engine = EmotionEngine()
    resolved = EmotionTurnPlanResolver(engine, enabled=False)(
        StructuredTurnPlan(
            emotion=EmotionLabel.focused,
            focused_variant=FocusedVariant.chuunibyou,
        )
    )

    assert resolved == StructuredTurnPlan(
        emotion=EmotionLabel.neutral,
        focused_variant=FocusedVariant.default,
    )


@pytest.mark.parametrize(
    "bounds",
    (
        {"max_bytes": True, "max_segments": 1},
        {"max_bytes": 1, "max_segments": False},
        {"max_bytes": 0, "max_segments": 1},
        {"max_bytes": 1, "max_segments": 0},
        {"max_bytes": 1, "max_segments": 1, "max_red_eye_segments": 0},
        {"max_bytes": 1, "max_segments": 1, "max_red_eye_segments": 2},
    ),
)
def test_parser_rejects_invalid_bounds(bounds: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        IncrementalTurnJSONParser(**bounds)


def test_parser_rejects_non_text_post_completion_and_bad_delimiters() -> None:
    parser = IncrementalTurnJSONParser(
        max_bytes=4096,
        max_segments=2,
        max_red_eye_segments=2,
    )
    with pytest.raises(LLMProviderError):
        parser.feed(1)  # type: ignore[arg-type]

    parser.feed(_payload(emotion="happy", variant="default"))
    with pytest.raises(LLMProviderError):
        parser.feed(" trailing")

    malformed = (
        "[]",
        '{"plan" }',
        '{"segments":{',
        '{"segments":[true',
        '{"segments":[{"text":"正文","red_eye":false} true',
        '{"plan":{"emotion":"happy","focused_variant":"default"} true',
    )
    for raw in malformed:
        candidate = IncrementalTurnJSONParser(
            max_bytes=4096,
            max_segments=2,
            max_red_eye_segments=2,
        )
        with pytest.raises(LLMProviderError):
            candidate.feed(raw)
            candidate.finish()


def test_stream_format_and_scanners_fail_closed_on_malformed_boundaries() -> None:
    class _Provider:
        turn_stream_format = "avatar_json"

    assert provider_turn_stream_format(_Provider()) is TurnStreamFormat.avatar_json
    assert provider_turn_stream_format(object()) is TurnStreamFormat.text

    class _InvalidProvider:
        turn_stream_format = object()

    assert provider_turn_stream_format(_InvalidProvider()) is TurnStreamFormat.text

    assert _scan_json_string("", 0) is None
    assert _scan_json_string('"escaped\\\\\\"quote"', 0) == len('"escaped\\\\\\"quote"')
    assert _scan_json_string('"unterminated', 0) is None
    with pytest.raises(LLMProviderError):
        _scan_json_string("not-a-string", 0)
    with pytest.raises(LLMProviderError):
        _scan_json_string('"control\u0001"', 0)

    assert _scan_compound("", 0, "{", "}") is None
    assert _scan_compound('{"nested":{"text":"}\\""}}', 0, "{", "}") == len(
        '{"nested":{"text":"}\\""}}'
    )
    assert _scan_compound('{"unterminated":', 0, "{", "}") is None
    with pytest.raises(LLMProviderError):
        _scan_compound("[]", 0, "{", "}")
    with pytest.raises(LLMProviderError):
        _scan_compound('{"wrong":[]}', 0, "{", "}")
    with pytest.raises(LLMProviderError):
        _scan_compound('{"control":"\u0001"}', 0, "{", "}")
