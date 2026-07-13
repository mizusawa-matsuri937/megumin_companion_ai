"""Provider-neutral context building seams."""

from __future__ import annotations

from typing import Protocol

from app.schemas import ChatMessage, ChatRequest, ChatRole, UserMessage


class ContextBuilder(Protocol):
    async def build(self, message: UserMessage) -> ChatRequest: ...


class DirectContextBuilder:
    """The privacy-minimal context used before optional memory is enabled."""

    async def build(self, message: UserMessage) -> ChatRequest:
        return ChatRequest(messages=[ChatMessage(role=ChatRole.user, content=message.text)])
