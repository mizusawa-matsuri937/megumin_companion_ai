"""Composable adapter from stored state to provider-neutral prompt inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.memory.models import MemoryType
from app.memory.service import HistoryService, MemoryService
from app.prompts.models import HistoryMessage
from app.schemas.ai import (
    ChatRole,
    ContextOrigin,
    ContextTrust,
    ExternalContextBlock,
)
from app.storage.records import ConversationRole


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    history: tuple[HistoryMessage, ...]
    blocks: tuple[ExternalContextBlock, ...]


class MemoryContextAssembler:
    def __init__(self, history: HistoryService, memory: MemoryService) -> None:
        self._history = history
        self._memory = memory

    def build(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        exclude_message_id: str | None = None,
        history_limit: int = 100,
        memory_limit: int = 10,
    ) -> ContextSnapshot:
        recent = self._history.recent(
            user_id=user_id,
            session_id=session_id,
            limit=history_limit,
            exclude_message_id=exclude_message_id,
        )
        history = tuple(
            HistoryMessage(
                message_id=item.message_id,
                role=(ChatRole.user if item.role is ConversationRole.user else ChatRole.assistant),
                content=item.content,
            )
            for item in recent
        )
        profiles = self._memory.profiles_for_context(user_id=user_id)
        profile_memory_ids = {item.memory_id for item in profiles}
        memories = self._memory.retrieve_for_context(
            user_id=user_id, query=query, limit=memory_limit
        )
        blocks: list[ExternalContextBlock] = [
            ExternalContextBlock(
                origin=ContextOrigin.user_profile,
                trust=ContextTrust.stored_fact,
                content=_json_content(
                    {
                        "key": profile.profile_key,
                        "value": profile.value,
                        "usage": "stable_fact",
                    }
                ),
                persistable=False,
                source_id=profile.memory_id,
            )
            for profile in profiles
        ]
        for memory in memories:
            if memory.memory_id in profile_memory_ids:
                continue
            usage = (
                "tone_only"
                if memory.memory_type in {MemoryType.emotion, MemoryType.relationship}
                else "fact_with_source"
            )
            blocks.append(
                ExternalContextBlock(
                    origin=ContextOrigin.long_term_memory,
                    trust=ContextTrust.stored_fact,
                    content=_json_content(
                        {
                            "type": memory.memory_type.value,
                            "content": memory.content,
                            "usage": usage,
                            "updated_at": memory.updated_at.isoformat(),
                        }
                    ),
                    persistable=False,
                    source_id=memory.memory_id,
                )
            )
        return ContextSnapshot(history=history, blocks=tuple(blocks))


def _json_content(value: dict[str, str]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
