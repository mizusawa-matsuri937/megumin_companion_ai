"""Strict incremental JSON contract for trusted, content-only assistant segments."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from app.clients.llm.errors import LLMErrorCode, LLMProviderError
from app.emotion import EmotionEngine, EmotionLabel, FocusedVariant
from app.schemas import ChatMessage, ChatRequest, ChatRole

STRUCTURED_TURN_INSTRUCTION = """\
只输出一个 JSON 对象，不要输出 Markdown、代码围栏或解释。对象必须严格符合：
{"plan":{"emotion":"neutral","focused_variant":"default"},"segments":[{"text":"中文正文","red_eye":false}]}
emotion 只能是 neutral、happy、shy、proud、angry_cute、worried、bored、excited、
explosion_mode、sleepy、focused。focused_variant 只能是 default 或 chuunibyou；
emotion 不是 focused 时必须为 default。segments 至少一项；text 以中文为主且不得包含控制标签；
red_eye 只能是布尔值。不要输出声音槽、模型路径、VTS 参数、动作名或 Hotkey。"""


class TurnStreamFormat(StrEnum):
    text = "text"
    avatar_json = "avatar_json"


class StructuredTurnPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    emotion: EmotionLabel
    focused_variant: FocusedVariant = FocusedVariant.default

    @model_validator(mode="after")
    def validate_variant(self) -> StructuredTurnPlan:
        if (
            self.emotion is not EmotionLabel.focused
            and self.focused_variant is not FocusedVariant.default
        ):
            raise ValueError("focused_variant requires focused emotion")
        return self


class StructuredTurnSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=20_000)
    red_eye: StrictBool = False

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("segment text cannot be blank")
        return normalized


class EmotionTurnPlanResolver:
    """Apply categorical LLM advice through the local cooldown owner."""

    def __init__(self, engine: EmotionEngine, *, enabled: bool = True) -> None:
        self._engine = engine
        self._enabled = enabled

    def __call__(self, suggestion: StructuredTurnPlan) -> StructuredTurnPlan:
        if self._enabled:
            emotion = self._engine.resolve_label_suggestion(suggestion.emotion).dominant_label
        else:
            emotion = self._engine.state.dominant_label
        variant = (
            suggestion.focused_variant
            if emotion is EmotionLabel.focused and suggestion.emotion is EmotionLabel.focused
            else FocusedVariant.default
        )
        return StructuredTurnPlan(emotion=emotion, focused_variant=variant)


class IncrementalTurnJSONParser:
    """Parse the two-field turn object while releasing only complete segment objects."""

    _TOP_LEVEL_KEYS = frozenset({"plan", "segments"})

    def __init__(
        self,
        *,
        max_bytes: int,
        max_segments: int,
        max_red_eye_segments: int = 8,
    ) -> None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
            or isinstance(max_segments, bool)
            or not isinstance(max_segments, int)
            or max_segments < 1
            or not 1 <= max_red_eye_segments <= max_segments
        ):
            raise ValueError("structured turn parser bounds are invalid")
        self._max_bytes = max_bytes
        self._max_segments = max_segments
        self._max_red_eye_segments = max_red_eye_segments
        self._buffer = ""
        self._cursor = 0
        self._state = "start"
        self._current_key: str | None = None
        self._seen: set[str] = set()
        self._plan: StructuredTurnPlan | None = None
        self._pending: list[StructuredTurnSegment] = []
        self._segment_count = 0
        self._red_eye_count = 0
        self._done = False

    @property
    def plan(self) -> StructuredTurnPlan | None:
        return self._plan

    @property
    def segment_count(self) -> int:
        return self._segment_count

    def feed(self, delta: str) -> list[StructuredTurnSegment]:
        if not isinstance(delta, str):
            raise _structured_error()
        if self._done and delta.strip():
            raise _structured_error()
        self._buffer += delta
        if len(self._buffer.encode("utf-8")) > self._max_bytes:
            raise _structured_error()
        output: list[StructuredTurnSegment] = []
        self._advance(output)
        return output

    def finish(self) -> list[StructuredTurnSegment]:
        output = self.feed("")
        if (
            not self._done
            or self._seen != self._TOP_LEVEL_KEYS
            or self._plan is None
            or self._segment_count < 1
            or self._pending
        ):
            raise _structured_error()
        return output

    def _advance(self, output: list[StructuredTurnSegment]) -> None:
        while True:
            self._skip_whitespace()
            if self._state == "start":
                if not self._available():
                    return
                self._require_character("{")
                self._state = "key_or_end"
                continue
            if self._state == "key_or_end":
                if not self._available():
                    return
                if self._peek() == "}":
                    self._cursor += 1
                    self._mark_done()
                    continue
                key_end = _scan_json_string(self._buffer, self._cursor)
                if key_end is None:
                    return
                try:
                    key = json.loads(self._buffer[self._cursor : key_end])
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise _structured_error() from exc
                if not isinstance(key, str) or key not in self._TOP_LEVEL_KEYS or key in self._seen:
                    raise _structured_error()
                self._seen.add(key)
                self._current_key = key
                self._cursor = key_end
                self._state = "colon"
                continue
            if self._state == "colon":
                if not self._available():
                    return
                self._require_character(":")
                self._state = "value"
                continue
            if self._state == "value":
                if self._current_key == "plan":
                    value_end = _scan_compound(self._buffer, self._cursor, "{", "}")
                    if value_end is None:
                        return
                    raw = self._decode_value(value_end)
                    try:
                        self._plan = StructuredTurnPlan.model_validate(raw)
                    except ValidationError as exc:
                        raise _structured_error() from exc
                    self._cursor = value_end
                    self._state = "after_value"
                    output.extend(self._release_pending())
                    continue
                if self._current_key == "segments":
                    if not self._available():
                        return
                    self._require_character("[")
                    self._state = "segment_or_end"
                    continue
                raise _structured_error()
            if self._state == "segment_or_end":
                if not self._available():
                    return
                if self._peek() == "]":
                    self._cursor += 1
                    self._state = "after_value"
                    continue
                value_end = _scan_compound(self._buffer, self._cursor, "{", "}")
                if value_end is None:
                    return
                raw = self._decode_value(value_end)
                try:
                    segment = StructuredTurnSegment.model_validate(raw)
                except ValidationError as exc:
                    raise _structured_error() from exc
                self._cursor = value_end
                self._accept_segment(segment, output)
                self._state = "segment_comma_or_end"
                continue
            if self._state == "segment_comma_or_end":
                if not self._available():
                    return
                character = self._peek()
                if character == ",":
                    self._cursor += 1
                    self._state = "segment_or_end"
                    continue
                if character == "]":
                    self._cursor += 1
                    self._state = "after_value"
                    continue
                raise _structured_error()
            if self._state == "after_value":
                if not self._available():
                    return
                character = self._peek()
                if character == ",":
                    self._cursor += 1
                    self._current_key = None
                    self._state = "key_or_end"
                    continue
                if character == "}":
                    self._cursor += 1
                    self._mark_done()
                    continue
                raise _structured_error()
            if self._state == "done":
                if self._available():
                    raise _structured_error()
                return
            raise _structured_error()

    def _accept_segment(
        self,
        segment: StructuredTurnSegment,
        output: list[StructuredTurnSegment],
    ) -> None:
        self._segment_count += 1
        if self._segment_count > self._max_segments:
            raise _structured_error()
        if segment.red_eye:
            self._red_eye_count += 1
            if self._red_eye_count > self._max_red_eye_segments:
                raise _structured_error()
        if self._plan is None:
            self._pending.append(segment)
        else:
            output.append(segment)

    def _release_pending(self) -> list[StructuredTurnSegment]:
        if self._plan is None:
            return []
        output = self._pending
        self._pending = []
        return output

    def _mark_done(self) -> None:
        self._state = "done"
        self._done = True

    def _decode_value(self, end: int) -> Any:
        try:
            return json.loads(self._buffer[self._cursor : end])
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise _structured_error() from exc

    def _skip_whitespace(self) -> None:
        while self._cursor < len(self._buffer) and self._buffer[self._cursor].isspace():
            self._cursor += 1

    def _available(self) -> bool:
        return self._cursor < len(self._buffer)

    def _peek(self) -> str:
        return self._buffer[self._cursor]

    def _require_character(self, expected: str) -> None:
        if self._peek() != expected:
            raise _structured_error()
        self._cursor += 1


def prepare_structured_turn_request(request: ChatRequest) -> ChatRequest:
    if len(request.messages) >= 200:
        raise _structured_error()
    instruction = ChatMessage(role=ChatRole.system, content=STRUCTURED_TURN_INSTRUCTION)
    insert_at = 0
    while insert_at < len(request.messages) and request.messages[insert_at].role is ChatRole.system:
        insert_at += 1
    messages = [
        *request.messages[:insert_at],
        instruction,
        *request.messages[insert_at:],
    ]
    return request.model_copy(
        update={
            "messages": messages,
            "response_format": "json_object",
        }
    )


def provider_turn_stream_format(provider: object) -> TurnStreamFormat:
    try:
        return TurnStreamFormat(getattr(provider, "turn_stream_format", TurnStreamFormat.text))
    except (TypeError, ValueError):
        return TurnStreamFormat.text


def _scan_json_string(value: str, start: int) -> int | None:
    if start >= len(value):
        return None
    if value[start] != '"':
        raise _structured_error()
    escaped = False
    for index in range(start + 1, len(value)):
        character = value[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            return index + 1
        if ord(character) < 0x20:
            raise _structured_error()
    return None


def _scan_compound(value: str, start: int, opening: str, closing: str) -> int | None:
    if start >= len(value):
        return None
    if value[start] != opening:
        raise _structured_error()
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        character = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            elif ord(character) < 0x20:
                raise _structured_error()
            continue
        if character == '"':
            in_string = True
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        elif character in "{}[]" and character not in {opening, closing}:
            raise _structured_error()
    return None


def _structured_error() -> LLMProviderError:
    return LLMProviderError(LLMErrorCode.structured, retryable=False)


TurnPlanResolver = Callable[[StructuredTurnPlan], StructuredTurnPlan]
