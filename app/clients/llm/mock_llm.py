"""A controllable token stream used before a real LLM is connected."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence

from app.core.cancellation import CancellationToken
from app.schemas import UserMessage

DEFAULT_RESPONSE = "哼哼，我收到了。Mock 链路正在正常工作！接下来也交给我吧。"


class MockLLMProvider:
    def __init__(
        self,
        *,
        deltas: Sequence[str] | None = None,
        response_factory: Callable[[UserMessage], str] | None = None,
        chunk_size: int = 2,
        first_token_delay_seconds: float = 0.0,
        token_delay_seconds: float = 0.02,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size 必须大于 0")
        if first_token_delay_seconds < 0 or token_delay_seconds < 0:
            raise ValueError("token 延迟不能为负数")
        self._deltas = tuple(deltas) if deltas is not None else None
        self._response_factory = response_factory or (lambda _message: DEFAULT_RESPONSE)
        self._chunk_size = chunk_size
        self._first_token_delay = first_token_delay_seconds
        self._token_delay = token_delay_seconds

    async def stream(self, message: UserMessage, token: CancellationToken) -> AsyncIterator[str]:
        deltas = self._deltas
        if deltas is None:
            response = self._response_factory(message)
            deltas = tuple(
                response[index : index + self._chunk_size]
                for index in range(0, len(response), self._chunk_size)
            )

        for index, delta in enumerate(deltas):
            delay = self._first_token_delay if index == 0 else self._token_delay
            if await token.wait_or_timeout(delay):
                return
            token.raise_if_cancelled()
            if delta:
                yield delta
