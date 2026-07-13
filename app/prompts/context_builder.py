"""Dialogue ContextBuilder adapter sharing deterministic emotion and prompt policy."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.emotion import Clock, EmotionEngine, EmotionStimulus, StimulusKind
from app.prompts.builder import PromptBuilder
from app.prompts.models import HistoryMessage
from app.schemas import ChatRequest, ExternalContextBlock, UserMessage


class PromptContextSource(Protocol):
    async def history_for(self, message: UserMessage) -> Sequence[HistoryMessage]: ...

    async def context_for(self, message: UserMessage) -> Sequence[ExternalContextBlock]: ...


class EmptyPromptContextSource:
    async def history_for(self, message: UserMessage) -> Sequence[HistoryMessage]:
        return ()

    async def context_for(self, message: UserMessage) -> Sequence[ExternalContextBlock]:
        return ()


class EmotionPromptContextBuilder:
    def __init__(
        self,
        prompt_builder: PromptBuilder,
        emotion_engine: EmotionEngine,
        clock: Clock,
        *,
        source: PromptContextSource | None = None,
        update_emotion: bool = True,
    ) -> None:
        self._prompt_builder = prompt_builder
        self._emotion_engine = emotion_engine
        self._clock = clock
        self._source = source or EmptyPromptContextSource()
        self._update_emotion = update_emotion

    async def build(self, message: UserMessage) -> ChatRequest:
        if self._update_emotion:
            occurred_at = max(self._clock.now(), self._emotion_engine.state.last_updated_at)
            self._emotion_engine.decay(at=occurred_at)
            kind = classify_stimulus(message.text)
            self._emotion_engine.apply(
                EmotionStimulus(
                    stimulus_id=message.message_id,
                    kind=kind,
                    occurred_at=occurred_at,
                    correlation_id=message.message_id,
                    reason_code=f"deterministic_text_{kind.value}",
                )
            )
        history = await self._source.history_for(message)
        context = await self._source.context_for(message)
        return self._prompt_builder.build(
            current_user_text=message.text,
            emotion=self._emotion_engine.state,
            history=history,
            context_blocks=context,
        )


def classify_stimulus(text: str) -> StimulusKind:
    normalized = text.casefold()
    rules: tuple[tuple[tuple[str, ...], StimulusKind], ...] = (
        (("累", "困", "tired", "sleepy"), StimulusKind.user_tired),
        (("难过", "焦虑", "害怕", "distress", "anxious"), StimulusKind.user_distress),
        (("爆裂", "爆炸", "explosion"), StimulusKind.explosion_topic),
        (("安静", "别说话", "quiet"), StimulusKind.quiet_request),
        (("专注", "工作中", "focus"), StimulusKind.focused_activity),
        (("游戏", "gaming", "game"), StimulusKind.gaming),
        (("谢谢", "厉害", "可爱", "great", "amazing"), StimulusKind.praise),
    )
    for keywords, kind in rules:
        if any(keyword in normalized for keyword in keywords):
            return kind
    return StimulusKind.neutral_interaction
