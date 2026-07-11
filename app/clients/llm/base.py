"""Provider-neutral streaming LLM contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.core.cancellation import CancellationToken
from app.schemas import UserMessage


class LLMProvider(Protocol):
    def stream(self, message: UserMessage, token: CancellationToken) -> AsyncIterator[str]: ...
