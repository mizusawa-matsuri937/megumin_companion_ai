"""Provider-neutral streaming LLM contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.core.cancellation import CancellationToken
from app.schemas import ChatCompletion, ChatRequest


class LLMProvider(Protocol):
    def stream(self, request: ChatRequest, token: CancellationToken) -> AsyncIterator[str]: ...

    async def complete(self, request: ChatRequest, token: CancellationToken) -> ChatCompletion: ...

    async def close(self) -> None: ...
