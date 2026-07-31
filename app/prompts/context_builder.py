"""Dialogue ContextBuilder adapter sharing deterministic emotion and prompt policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.emotion import Clock, EmotionEngine, EmotionStimulus, StimulusKind
from app.prompts.builder import PromptBuilder
from app.prompts.models import HistoryMessage
from app.schemas import ChatRequest, ExternalContextBlock, ProactiveIntent, UserMessage


@dataclass(frozen=True, slots=True)
class PromptContextSnapshot:
    """One atomic view of all revocable context for a pending LLM request."""

    history: tuple[HistoryMessage, ...] = ()
    blocks: tuple[ExternalContextBlock, ...] = ()


class PromptContextSource(Protocol):
    async def snapshot_for(self, message: UserMessage) -> PromptContextSnapshot: ...


class EmptyPromptContextSource:
    async def snapshot_for(self, message: UserMessage) -> PromptContextSnapshot:
        return PromptContextSnapshot()


class CompositePromptContextSource:
    """Combine independent, policy-scoped prompt sources without reordering them."""

    def __init__(self, *sources: PromptContextSource) -> None:
        self._sources = sources

    async def snapshot_for(self, message: UserMessage) -> PromptContextSnapshot:
        history: list[HistoryMessage] = []
        blocks: list[ExternalContextBlock] = []
        for source in self._sources:
            snapshot = await source.snapshot_for(message)
            history.extend(snapshot.history)
            blocks.extend(snapshot.blocks)
        return PromptContextSnapshot(history=tuple(history), blocks=tuple(blocks))


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
        snapshot = await self._source.snapshot_for(message)
        return self._prompt_builder.build(
            current_user_text=message.text,
            emotion=self._emotion_engine.state,
            history=snapshot.history,
            context_blocks=snapshot.blocks,
        )

    async def build_proactive(self, intent: ProactiveIntent) -> ChatRequest:
        return self._prompt_builder.build_proactive(
            intent=intent,
            emotion=self._emotion_engine.state,
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
