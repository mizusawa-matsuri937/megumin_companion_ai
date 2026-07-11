"""Incremental multilingual sentence segmentation for short spoken replies."""

from __future__ import annotations

import re

from app.schemas import DialogueSegment

_ABBREVIATIONS = {
    "dr",
    "e.g",
    "etc",
    "i.e",
    "jr",
    "mr",
    "mrs",
    "ms",
    "prof",
    "sr",
    "st",
    "vs",
}
_REACTIONS = {
    "啊！",
    "啊？",
    "え？",
    "えっ？",
    "嗯！",
    "嗯？",
    "哼！",
    "呀！",
    "诶！",
    "诶？",
    "哦！",
    "哦？",
    "欸？",
}
_CLOSERS = "\"'”’」』】）》〕〉）]}"
_HARD_TERMINATORS = "。！？!?；;"
_WEAK_BREAKS = "，、,：:"
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


class DialogueSegmenter:
    """Turn streaming deltas into natural, bounded ``DialogueSegment`` objects."""

    def __init__(
        self,
        turn_id: str,
        *,
        min_chars: int = 6,
        max_chars: int = 42,
        max_words: int = 25,
    ) -> None:
        if min_chars < 1:
            raise ValueError("min_chars 必须大于 0")
        if max_chars < min_chars:
            raise ValueError("max_chars 必须不小于 min_chars")
        if max_words < 1:
            raise ValueError("max_words 必须大于 0")
        self._turn_id = turn_id
        self._min_chars = min_chars
        self._max_chars = max_chars
        self._max_words = max_words
        self._buffer = ""
        self._next_index = 0

    @property
    def buffered_text(self) -> str:
        return self._buffer

    def feed(self, delta: str) -> list[DialogueSegment]:
        if not delta:
            return []
        self._buffer += delta
        return self._drain(final=False)

    def flush(self) -> list[DialogueSegment]:
        segments = self._drain(final=True)
        remainder = self._buffer.strip()
        self._buffer = ""
        if remainder:
            segments.append(self._make_segment(remainder))
        return segments

    def _drain(self, *, final: bool) -> list[DialogueSegment]:
        segments: list[DialogueSegment] = []
        while self._buffer:
            boundary = self._sentence_boundary(final=final)
            if boundary is not None:
                candidate = self._buffer[:boundary].strip()
                if self._can_emit(candidate):
                    self._buffer = self._buffer[boundary:].lstrip()
                    segments.append(self._make_segment(candidate))
                    continue

            forced = self._forced_boundary()
            if forced is None:
                break
            candidate = self._buffer[:forced].strip()
            self._buffer = self._buffer[forced:].lstrip()
            if candidate:
                segments.append(self._make_segment(candidate))
        return segments

    def _sentence_boundary(self, *, final: bool) -> int | None:
        for index, character in enumerate(self._buffer):
            is_boundary = character in _HARD_TERMINATORS
            if character == ".":
                is_boundary = self._period_is_terminal(index, final=final)
            elif character == "…":
                is_boundary = self._ellipsis_is_terminal(index, final=final)
            if not is_boundary:
                continue
            end = index + 1
            while end < len(self._buffer) and self._buffer[end] in _CLOSERS:
                end += 1
            if self._can_emit(self._buffer[:end].strip()):
                return end
        return None

    def _period_is_terminal(self, index: int, *, final: bool) -> bool:
        before = self._buffer[index - 1] if index else ""
        after = self._buffer[index + 1] if index + 1 < len(self._buffer) else ""

        if before == "." or after == ".":
            run_start = index
            while run_start and self._buffer[run_start - 1] == ".":
                run_start -= 1
            run_end = index + 1
            while run_end < len(self._buffer) and self._buffer[run_end] == ".":
                run_end += 1
            return run_end - run_start >= 3 and index == run_end - 1

        if before.isdigit() and after.isdigit():
            return False

        prefix = self._buffer[:index]
        word_match = re.search(r"([A-Za-z]+)$", prefix)
        if word_match and word_match.group(1).lower() in _ABBREVIATIONS:
            return False
        if re.search(r"(?:\b[A-Za-z]\.)+[A-Za-z]$", prefix):
            return False
        if word_match and len(word_match.group(1)) == 1 and word_match.group(1).isupper():
            return False

        # A period at the streaming edge is ambiguous until another delta or flush arrives.
        return bool(after) or final

    def _ellipsis_is_terminal(self, index: int, *, final: bool) -> bool:
        run_start = index
        while run_start and self._buffer[run_start - 1] == "…":
            run_start -= 1
        run_end = index + 1
        while run_end < len(self._buffer) and self._buffer[run_end] == "…":
            run_end += 1
        run_length = run_end - run_start
        return index == run_end - 1 and (run_length >= 2 or final)

    def _can_emit(self, candidate: str) -> bool:
        compact = re.sub(r"[\W_]", "", candidate, flags=re.UNICODE)
        return len(compact) >= self._min_chars or candidate in _REACTIONS

    def _forced_boundary(self) -> int | None:
        words = list(_WORD_RE.finditer(self._buffer))
        word_boundary: int | None = None
        if len(words) > self._max_words:
            word_boundary = words[self._max_words - 1].end()

        if len(self._buffer) <= self._max_chars and word_boundary is None:
            return None

        limit = min(self._max_chars, word_boundary or self._max_chars)
        minimum = min(self._min_chars, limit)
        for separators in (_WEAK_BREAKS, " \t\n"):
            positions = [
                index + 1
                for index, character in enumerate(self._buffer[:limit])
                if character in separators and index + 1 >= minimum
            ]
            if positions:
                return positions[-1]
        return limit

    def _make_segment(self, text: str) -> DialogueSegment:
        segment = DialogueSegment(turn_id=self._turn_id, index=self._next_index, text=text)
        self._next_index += 1
        return segment
