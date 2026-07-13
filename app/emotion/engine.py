"""Deterministic bounded emotion state machine."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import datetime, timedelta

from app.emotion.clock import Clock, SystemClock
from app.emotion.models import (
    EmotionDimension,
    EmotionLabel,
    EmotionState,
    EmotionStimulus,
    EmotionSuggestion,
    EmotionTransition,
    StimulusKind,
)

_RULES: dict[StimulusKind, dict[EmotionDimension, float]] = {
    StimulusKind.praise: {
        EmotionDimension.embarrassment: 0.10,
        EmotionDimension.affection: 0.02,
        EmotionDimension.pride: 0.04,
    },
    StimulusKind.user_distress: {
        EmotionDimension.concern: 0.15,
        EmotionDimension.affection: 0.03,
    },
    StimulusKind.user_tired: {
        EmotionDimension.concern: 0.10,
        EmotionDimension.energy: -0.03,
    },
    StimulusKind.explosion_topic: {
        EmotionDimension.explosion_urge: 0.12,
        EmotionDimension.energy: 0.05,
    },
    StimulusKind.quiet_request: {EmotionDimension.boredom: -0.05},
    StimulusKind.repeated_interruption: {EmotionDimension.concern: 0.04},
    StimulusKind.focused_activity: {EmotionDimension.curiosity: 0.03},
    StimulusKind.gaming: {
        EmotionDimension.energy: 0.05,
        EmotionDimension.explosion_urge: 0.05,
    },
    StimulusKind.late_night: {
        EmotionDimension.concern: 0.12,
        EmotionDimension.energy: -0.05,
    },
    StimulusKind.inactivity: {EmotionDimension.boredom: 0.08},
    StimulusKind.neutral_interaction: {EmotionDimension.boredom: -0.03},
    StimulusKind.time_decay: {},
}

_NEUTRALS: dict[EmotionDimension, float] = {
    EmotionDimension.affection: 0.35,
    EmotionDimension.energy: 0.55,
    EmotionDimension.curiosity: 0.45,
    EmotionDimension.concern: 0.15,
    EmotionDimension.embarrassment: 0.10,
    EmotionDimension.pride: 0.55,
    EmotionDimension.explosion_urge: 0.35,
    EmotionDimension.boredom: 0.20,
}

# Fraction of the remaining distance to neutral per minute.
_DECAY_RATES: dict[EmotionDimension, float] = {
    EmotionDimension.affection: 0.002,
    EmotionDimension.energy: 0.04,
    EmotionDimension.curiosity: 0.05,
    EmotionDimension.concern: 0.08,
    EmotionDimension.embarrassment: 0.15,
    EmotionDimension.pride: 0.01,
    EmotionDimension.explosion_urge: 0.05,
    EmotionDimension.boredom: 0.05,
}


class EmotionEngine:
    """Apply explainable stimuli while enforcing all numeric and temporal limits."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        initial: EmotionState | None = None,
        max_delta_per_event: float = 0.15,
        max_delta_per_minute: float = 0.25,
        label_min_duration: timedelta = timedelta(seconds=15),
        explosion_cooldown: timedelta = timedelta(minutes=5),
    ) -> None:
        if not 0 < max_delta_per_event <= 1:
            raise ValueError("max_delta_per_event must be in (0, 1]")
        if not 0 < max_delta_per_minute <= 1:
            raise ValueError("max_delta_per_minute must be in (0, 1]")
        if label_min_duration.total_seconds() < 0 or explosion_cooldown.total_seconds() < 0:
            raise ValueError("cooldowns cannot be negative")
        self._clock = clock or SystemClock()
        now = self._clock.now()
        self._state = initial or EmotionState(last_updated_at=now, label_since=now)
        self._max_event = max_delta_per_event
        self._max_minute = max_delta_per_minute
        self._label_min_duration = label_min_duration
        self._explosion_cooldown = explosion_cooldown
        self._last_explosion_at: datetime | None = None
        self._focus_until: datetime | None = None
        self._rolling: dict[EmotionDimension, deque[tuple[datetime, float]]] = defaultdict(deque)

    @property
    def state(self) -> EmotionState:
        return self._state

    def apply(self, stimulus: EmotionStimulus) -> EmotionTransition:
        if stimulus.kind is StimulusKind.time_decay:
            raise ValueError("use decay() for time decay")
        if stimulus.occurred_at < self._state.last_updated_at:
            raise ValueError("stimuli must be applied in chronological order")
        requested = {
            dimension: delta * stimulus.intensity
            for dimension, delta in _RULES[stimulus.kind].items()
        }
        if stimulus.kind is StimulusKind.focused_activity:
            self._focus_until = stimulus.occurred_at + timedelta(minutes=5)
        return self._transition(
            stimulus_id=stimulus.stimulus_id,
            kind=stimulus.kind,
            occurred_at=stimulus.occurred_at,
            requested=requested,
            reason_code=stimulus.reason_code,
        )

    def apply_suggestion(self, suggestion: EmotionSuggestion) -> EmotionTransition:
        if suggestion.kind is StimulusKind.time_decay:
            raise ValueError("LLM suggestion cannot request time decay")
        return self.apply(
            EmotionStimulus(
                stimulus_id=suggestion.suggestion_id,
                kind=suggestion.kind,
                intensity=min(0.5, suggestion.intensity * suggestion.confidence),
                occurred_at=suggestion.occurred_at,
                reason_code=f"llm_suggestion_{suggestion.kind.value}",
            )
        )

    def decay(self, *, at: datetime | None = None) -> EmotionTransition:
        occurred_at = at or self._clock.now()
        if occurred_at < self._state.last_updated_at:
            raise ValueError("decay time cannot move backwards")
        elapsed_minutes = (occurred_at - self._state.last_updated_at).total_seconds() / 60
        requested: dict[EmotionDimension, float] = {}
        if elapsed_minutes > 0:
            for dimension, neutral in _NEUTRALS.items():
                current = self._state.value(dimension)
                factor = 1 - (1 - _DECAY_RATES[dimension]) ** elapsed_minutes
                requested[dimension] = (neutral - current) * factor
        return self._transition(
            stimulus_id=f"decay-{occurred_at.isoformat()}",
            kind=StimulusKind.time_decay,
            occurred_at=occurred_at,
            requested=requested,
            reason_code="time_decay",
        )

    def _transition(
        self,
        *,
        stimulus_id: str,
        kind: StimulusKind,
        occurred_at: datetime,
        requested: Mapping[EmotionDimension, float],
        reason_code: str,
    ) -> EmotionTransition:
        before = self._state
        updates: dict[str, object] = {}
        applied: dict[EmotionDimension, float] = {}
        limited: set[EmotionDimension] = set()
        for dimension, raw_delta in requested.items():
            delta, was_limited = self._limit_delta(dimension, raw_delta, occurred_at)
            current = before.value(dimension)
            updated = min(1.0, max(0.0, current + delta))
            actual = updated - current
            if abs(actual - raw_delta) > 1e-12:
                was_limited = True
            updates[dimension.value] = updated
            applied[dimension] = actual
            if was_limited:
                limited.add(dimension)
            if actual:
                self._rolling[dimension].append((occurred_at, actual))

        provisional = before.model_copy(
            update={**updates, "last_updated_at": occurred_at, "reason_code": reason_code}
        )
        candidate = self._map_label(provisional, kind, occurred_at)
        label_changed = False
        suppressed = False
        if candidate is not before.dominant_label:
            can_change = (
                before.dominant_label is EmotionLabel.neutral
                or occurred_at - before.label_since >= self._label_min_duration
            )
            if candidate is EmotionLabel.explosion_mode and self._last_explosion_at is not None:
                can_change = can_change and (
                    occurred_at - self._last_explosion_at >= self._explosion_cooldown
                )
            if can_change:
                updates["dominant_label"] = candidate
                updates["label_since"] = occurred_at
                label_changed = True
                if candidate is EmotionLabel.explosion_mode:
                    self._last_explosion_at = occurred_at
            else:
                suppressed = True

        intensity = max((abs(value) for value in applied.values()), default=0.0) / self._max_event
        updates.update(
            {
                "intensity": min(1.0, intensity),
                "last_updated_at": occurred_at,
                "reason_code": reason_code,
            }
        )
        self._state = before.model_copy(update=updates)
        return EmotionTransition(
            stimulus_id=stimulus_id,
            kind=kind,
            before=before,
            after=self._state,
            requested_delta=dict(requested),
            applied_delta=applied,
            limited_dimensions=frozenset(limited),
            label_changed=label_changed,
            label_change_suppressed=suppressed,
            reason_code=reason_code,
        )

    def _limit_delta(
        self, dimension: EmotionDimension, requested: float, occurred_at: datetime
    ) -> tuple[float, bool]:
        event_limited = min(self._max_event, max(-self._max_event, requested))
        entries = self._rolling[dimension]
        cutoff = occurred_at - timedelta(minutes=1)
        while entries and entries[0][0] <= cutoff:
            entries.popleft()
        remaining = max(0.0, self._max_minute - sum(abs(value) for _at, value in entries))
        if event_limited >= 0:
            minute_limited = min(event_limited, remaining)
        else:
            minute_limited = max(event_limited, -remaining)
        return minute_limited, abs(minute_limited - requested) > 1e-12

    def _map_label(
        self, state: EmotionState, stimulus_kind: StimulusKind, occurred_at: datetime
    ) -> EmotionLabel:
        if self._focus_until is not None and occurred_at < self._focus_until:
            return EmotionLabel.focused
        if state.energy < 0.22:
            return EmotionLabel.sleepy
        if state.explosion_urge > 0.82 and state.energy > 0.55:
            return EmotionLabel.explosion_mode
        if state.concern > 0.65:
            return EmotionLabel.worried
        if state.embarrassment > 0.60:
            return EmotionLabel.shy
        if state.boredom > 0.70:
            return EmotionLabel.bored
        if state.curiosity > 0.65 and state.energy > 0.45:
            return EmotionLabel.excited
        if state.pride > 0.65:
            return EmotionLabel.proud
        if state.affection > 0.60:
            return EmotionLabel.happy
        return EmotionLabel.neutral
