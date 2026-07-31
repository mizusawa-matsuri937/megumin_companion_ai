"""Focused policy tests for the text-only perception prompt adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from app.emotion import FakeClock
from app.perception.guards import TextRedactor
from app.perception.prompt_context import (
    ApprovedVisualSummary,
    PerceptionPromptContextSource,
    VisualSummaryLabel,
)
from app.prompts import (
    CompositePromptContextSource,
    HistoryMessage,
    PromptContextSnapshot,
)
from app.schemas import (
    ChatRole,
    ExternalContextBlock,
    FeatureActualState,
    FeatureDesiredState,
    FeatureName,
    FeatureState,
    PerceptionContext,
    UserMessage,
)
from app.schemas.ai import ContextOrigin, ContextTrust

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


class _Flags:
    def __init__(self, *, vision_enabled: bool = True) -> None:
        self.vision_enabled = vision_enabled

    def get_feature(self, name: FeatureName) -> FeatureState:
        assert name is FeatureName.vision
        return FeatureState(name=name, enabled=self.vision_enabled, updated_at=NOW)

    def subscribe(self, _listener: Callable[[FeatureState], None]) -> Callable[[], None]:
        raise AssertionError("the prompt source must use its explicit transition barrier")


class _BlankRedactor(TextRedactor):
    def redact(self, value: str) -> str:
        del value
        return "   "


class _StaticSource:
    def __init__(self, snapshot: PromptContextSnapshot) -> None:
        self._snapshot = snapshot

    async def snapshot_for(self, _message: UserMessage) -> PromptContextSnapshot:
        return self._snapshot


def _message(*, allowed: bool = True) -> UserMessage:
    return UserMessage(text="请结合上下文回复", screen_context_allowed=allowed)


def _context(
    *,
    labels: tuple[VisualSummaryLabel, ...] = (VisualSummaryLabel.no_relevant_change,),
    observed_at: datetime = NOW,
    sensitive: bool = False,
) -> ApprovedVisualSummary:
    return ApprovedVisualSummary(
        labels=labels,
        observed_at=observed_at,
        sensitive=sensitive,
    )


def _source(
    flags: _Flags,
    *,
    clock: FakeClock | None = None,
    redactor: TextRedactor | None = None,
) -> PerceptionPromptContextSource:
    return PerceptionPromptContextSource(
        flags,
        max_age=timedelta(seconds=30),
        clock=clock or FakeClock(NOW),
        redactor=redactor,
    )


def test_safe_opted_in_semantic_summary_is_untrusted_and_nonpersistable() -> None:
    async def scenario() -> None:
        flags = _Flags()
        source = _source(flags)
        source.publish_approved(_context(labels=(VisualSummaryLabel.non_sensitive_change,)))

        snapshot = await source.snapshot_for(_message())

        assert snapshot.history == ()
        assert len(snapshot.blocks) == 1
        block = snapshot.blocks[0]
        assert block.origin is ContextOrigin.screen
        assert block.trust is ContextTrust.untrusted_observation
        assert not block.persistable
        assert block.source_id is None
        assert json.loads(block.content) == {"summary": "检测到非敏感视觉变化。"}

    asyncio.run(scenario())


def test_visual_context_requires_opt_in_enabled_feature_and_fresh_safe_summary() -> None:
    async def scenario() -> None:
        flags = _Flags()
        clock = FakeClock(NOW)
        source = _source(flags, clock=clock)
        source.publish_approved(_context())

        assert await source.snapshot_for(_message(allowed=False)) == PromptContextSnapshot()
        flags.vision_enabled = False
        assert await source.snapshot_for(_message()) == PromptContextSnapshot()

        flags.vision_enabled = True
        source.publish_approved(_context(sensitive=True))
        assert await source.snapshot_for(_message()) == PromptContextSnapshot()

        source.publish_approved(_context())
        clock.advance(timedelta(seconds=31))
        assert await source.snapshot_for(_message()) == PromptContextSnapshot()

        future_source = _source(_Flags())
        future_source.publish_approved(_context(observed_at=NOW + timedelta(seconds=1)))
        assert await future_source.snapshot_for(_message()) == PromptContextSnapshot()

    asyncio.run(scenario())


def test_blank_redaction_is_omitted_instead_of_creating_an_invalid_block() -> None:
    async def scenario() -> None:
        source = _source(_Flags(), redactor=_BlankRedactor())
        source.publish_approved(_context())

        assert await source.snapshot_for(_message()) == PromptContextSnapshot()

    asyncio.run(scenario())


def test_vision_disable_revokes_summary_and_rejects_pre_disable_observations() -> None:
    async def scenario() -> None:
        flags = _Flags()
        clock = FakeClock(NOW)
        source = _source(flags, clock=clock)
        source.publish_approved(_context())
        assert len((await source.snapshot_for(_message())).blocks) == 1

        flags.vision_enabled = False
        await source.apply_feature_state(
            FeatureState(
                name=FeatureName.vision,
                desired_state=FeatureDesiredState.disabled,
                actual_state=FeatureActualState.disabling,
                updated_at=NOW,
            )
        )
        assert await source.snapshot_for(_message()) == PromptContextSnapshot()

        flags.vision_enabled = True
        await source.apply_feature_state(
            FeatureState(name=FeatureName.vision, enabled=True, updated_at=NOW)
        )
        source.publish_approved(_context(observed_at=NOW))
        assert await source.snapshot_for(_message()) == PromptContextSnapshot()

        clock.advance(timedelta(seconds=1))
        source.publish_approved(_context(observed_at=clock.now()))
        assert len((await source.snapshot_for(_message())).blocks) == 1

    asyncio.run(scenario())


def test_visual_prompt_source_rejects_raw_perception_ocr_title_and_asset_text() -> None:
    async def scenario() -> None:
        source = _source(_Flags())
        raw = PerceptionContext(
            observation_id="https://example.invalid/capture.png?ocr=raw-ocr-sentinel",
            summary="raw-ocr-sentinel should never become remote context",
            observed_at=NOW,
        )
        source.publish_approved(cast(ApprovedVisualSummary, raw))
        assert await source.snapshot_for(_message()) == PromptContextSnapshot()

        for raw_text in (
            "raw-ocr-sentinel from a private screenshot",
            "private window title sentinel",
            r"screenshot_path=C:\\Users\\alice\\private.png",
            "asset=ms-appx://Megumin/private.png",
        ):
            with pytest.raises(ValueError, match="vocabulary"):
                _context(labels=cast(tuple[VisualSummaryLabel, ...], (raw_text,)))
        assert await source.snapshot_for(_message()) == PromptContextSnapshot()

    asyncio.run(scenario())


def test_composite_source_preserves_history_and_source_order() -> None:
    async def scenario() -> None:
        memory = _StaticSource(
            PromptContextSnapshot(
                history=(
                    HistoryMessage(
                        message_id="old-assistant",
                        role=ChatRole.assistant,
                        content="旧回答",
                    ),
                ),
                blocks=(
                    ExternalContextBlock(
                        origin=ContextOrigin.long_term_memory,
                        trust=ContextTrust.stored_fact,
                        content="长期记忆",
                    ),
                ),
            )
        )
        screen = _StaticSource(
            PromptContextSnapshot(
                blocks=(
                    ExternalContextBlock(
                        origin=ContextOrigin.screen,
                        trust=ContextTrust.untrusted_observation,
                        content="视觉摘要",
                    ),
                ),
            )
        )

        snapshot = await CompositePromptContextSource(memory, screen).snapshot_for(_message())

        assert tuple(item.message_id for item in snapshot.history) == ("old-assistant",)
        assert tuple(item.origin for item in snapshot.blocks) == (
            ContextOrigin.long_term_memory,
            ContextOrigin.screen,
        )

    asyncio.run(scenario())
