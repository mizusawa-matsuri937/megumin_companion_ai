"""Expose only approved, fresh visual-summary text to dialogue prompts.

This adapter deliberately sits after a future privacy-reviewed perception
boundary.  It accepts no generic :class:`PerceptionContext`, frames, OCR
spans, window titles, identifiers, or image URLs.  The explicit approved
summary capability contains only finite semantic labels and time metadata
needed for one prompt turn.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from threading import RLock

from app.core import FeatureFlagSource
from app.emotion import Clock, SystemClock
from app.perception.guards import TextRedactor
from app.prompts.context_builder import PromptContextSnapshot, PromptContextSource
from app.schemas import (
    ExternalContextBlock,
    FeatureName,
    FeatureState,
    UserMessage,
)
from app.schemas.ai import ContextOrigin, ContextTrust

_DISALLOWED_REMOTE_REFERENCE = re.compile(
    r"(?i)(?:\b[a-z][a-z0-9+.-]{1,31}:(?://|[^\s]+)|\bdata\s*:\s*image/|"
    r"(?:^|[\s=])www\.\S+|\b[a-z]:[\\/]|\\\\[^\s]+|(?:^|[\s=])/[^\s]+)"
)


class VisualSummaryLabel(StrEnum):
    """The only non-sensitive visual facts W30 may turn into remote text."""

    no_relevant_change = "no_relevant_change"
    non_sensitive_change = "non_sensitive_change"
    attention_recommended = "attention_recommended"


_VISUAL_SUMMARY_LABEL_TEXT: dict[VisualSummaryLabel, str] = {
    VisualSummaryLabel.no_relevant_change: "未检测到需要参考的非敏感视觉变化。",
    VisualSummaryLabel.non_sensitive_change: "检测到非敏感视觉变化。",
    VisualSummaryLabel.attention_recommended: "检测到可由用户自行确认的非敏感视觉变化。",
}


@dataclass(frozen=True, slots=True)
class ApprovedVisualSummary:
    """A finite privacy-bound semantic capability for a future producer.

    Free-form summary text is intentionally not accepted: without a reviewed
    producer, the adapter cannot distinguish a semantic summary from OCR,
    window-title, or asset text.  The small fixed vocabulary is rendered here,
    after provenance has been reduced to non-sensitive semantic labels.
    """

    labels: tuple[VisualSummaryLabel, ...]
    observed_at: datetime
    sensitive: bool = False

    def __post_init__(self) -> None:
        if (
            not self.labels
            or len(self.labels) > 4
            or any(not isinstance(label, VisualSummaryLabel) for label in self.labels)
            or len(set(self.labels)) != len(self.labels)
        ):
            raise ValueError("approved visual summary labels must be a short unique vocabulary")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("approved visual summary time must be timezone-aware")
        if not isinstance(self.sensitive, bool):
            raise ValueError("approved visual summary sensitivity must be boolean")

    @property
    def prompt_text(self) -> str:
        return "；".join(_VISUAL_SUMMARY_LABEL_TEXT[label] for label in self.labels)


@dataclass(frozen=True, slots=True)
class _PublishedSummary:
    summary: str
    observed_at: datetime


class PerceptionPromptContextSource(PromptContextSource):
    """Retain one revocable, text-only visual summary for an opted-in turn."""

    def __init__(
        self,
        features: FeatureFlagSource,
        *,
        max_age: timedelta,
        clock: Clock | None = None,
        redactor: TextRedactor | None = None,
    ) -> None:
        if max_age <= timedelta(0):
            raise ValueError("perception prompt context max age must be positive")
        self._features = features
        self._max_age = max_age
        self._clock = clock or SystemClock()
        self._redactor = redactor or TextRedactor()
        self._lock = RLock()
        self._latest: _PublishedSummary | None = None
        self._epoch = 0
        self._not_before: datetime | None = None

    def publish_approved(self, approved: ApprovedVisualSummary | None) -> None:
        """Publish one approved summary without retaining IDs or raw perception.

        A sensitive, absent, or invalid capability is a revocation event.  The
        generated fixed-vocabulary text is redacted again at this outbound
        boundary and rejected if it contains a URL or path-like reference.
        Generic ``PerceptionContext`` and arbitrary summary text are not an
        accepted input type, so W30 cannot send OCR or window-title text.
        """

        if (
            not isinstance(approved, ApprovedVisualSummary)
            or approved.sensitive
            or not self._vision_enabled()
        ):
            self.invalidate()
            return
        try:
            summary = self._redactor.redact(approved.prompt_text).strip()
        except Exception:
            self.invalidate()
            return
        if not summary or _DISALLOWED_REMOTE_REFERENCE.search(summary) is not None:
            self.invalidate()
            return
        with self._lock:
            not_before = self._not_before
            if not_before is not None and approved.observed_at <= not_before:
                return
            self._latest = _PublishedSummary(
                summary=summary,
                observed_at=approved.observed_at,
            )
            self._epoch += 1

    async def apply_feature_state(self, state: FeatureState) -> None:
        """Turn a vision disable transition into a prompt-context barrier."""

        if state.name is not FeatureName.vision or state.desired_enabled:
            return
        now = self._clock.now()
        with self._lock:
            if self._not_before is None or now > self._not_before:
                self._not_before = now
            self._latest = None
            self._epoch += 1

    def invalidate(self) -> None:
        """Forget the latest summary without retaining a reason or content."""

        with self._lock:
            self._latest = None
            self._epoch += 1

    async def snapshot_for(self, message: UserMessage) -> PromptContextSnapshot:
        if not message.screen_context_allowed:
            return PromptContextSnapshot()
        while True:
            if not self._vision_enabled():
                return PromptContextSnapshot()
            with self._lock:
                epoch = self._epoch
                latest = self._latest
            if latest is None:
                return PromptContextSnapshot()
            if not self._is_fresh(latest):
                self._discard_if_current(epoch, latest)
                return PromptContextSnapshot()
            block = ExternalContextBlock(
                origin=ContextOrigin.screen,
                trust=ContextTrust.untrusted_observation,
                content=json.dumps(
                    {"summary": latest.summary},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                persistable=False,
            )
            if not self._vision_enabled():
                continue
            with self._lock:
                if epoch == self._epoch:
                    return PromptContextSnapshot(blocks=(block,))

    def _discard_if_current(self, epoch: int, latest: _PublishedSummary) -> None:
        with self._lock:
            if epoch == self._epoch and self._latest == latest:
                self._latest = None
                self._epoch += 1

    def _is_fresh(self, latest: _PublishedSummary) -> bool:
        age = self._clock.now() - latest.observed_at
        return timedelta(0) <= age <= self._max_age

    def _vision_enabled(self) -> bool:
        try:
            return self._features.get_feature(FeatureName.vision).enabled
        except Exception:
            return False
