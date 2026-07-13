"""Deterministic sliding-window limiter for optional cloud vision calls."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        max_calls: int,
        period_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_calls < 1 or period_seconds <= 0:
            raise ValueError("限流参数必须大于 0")
        self._max_calls = max_calls
        self._period_seconds = period_seconds
        self._clock = clock
        self._calls: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        boundary = now - self._period_seconds
        while self._calls and self._calls[0] <= boundary:
            self._calls.popleft()
        if len(self._calls) >= self._max_calls:
            return False
        self._calls.append(now)
        return True
