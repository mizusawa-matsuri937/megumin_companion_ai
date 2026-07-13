from datetime import UTC, datetime, timedelta

import pytest
from app.emotion.clock import FakeClock, SystemClock
from app.emotion.models import EmotionState, EmotionStimulus, StimulusKind
from pydantic import ValidationError


def test_state_rejects_out_of_range_values() -> None:
    now = datetime(2026, 7, 13, tzinfo=UTC)
    with pytest.raises(ValidationError):
        EmotionState(energy=1.01, last_updated_at=now, label_since=now)


def test_domain_timestamps_must_be_aware() -> None:
    naive = datetime(2026, 7, 13)
    with pytest.raises(ValidationError, match="timezone-aware"):
        EmotionStimulus(
            stimulus_id="stim",
            kind=StimulusKind.praise,
            occurred_at=naive,
            reason_code="test",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(naive)


def test_clock_controls_are_monotonic_and_system_clock_is_aware() -> None:
    start = datetime(2026, 7, 13, tzinfo=UTC)
    clock = FakeClock(start)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    clock.set(start + timedelta(seconds=1))
    assert clock.now() == start + timedelta(seconds=1)
    with pytest.raises(ValueError, match="backwards"):
        clock.set(start)
    assert SystemClock().now().tzinfo is not None
