"""Injectable wall clock used by deterministic emotion and memory tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self._current = _aware(current)

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        if delta.total_seconds() < 0:
            raise ValueError("fake clock cannot move backwards")
        self._current += delta
        return self._current

    def set(self, value: datetime) -> None:
        value = _aware(value)
        if value < self._current:
            raise ValueError("fake clock cannot move backwards")
        self._current = value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock timestamps must be timezone-aware")
    return value
