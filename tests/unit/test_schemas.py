"""Serialization and validation for the first five message contracts."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.schemas import AudioResult, DialogueSegment, InputMode, TTSJob, TurnState, UserMessage
from pydantic import ValidationError


@pytest.mark.parametrize("input_mode", ["text", "voice"])
def test_user_message_supports_both_input_modes(input_mode: str) -> None:
    message = UserMessage(text="  测试消息  ", input_mode=InputMode(input_mode))
    serialized = message.model_dump(mode="json")

    assert message.text == "测试消息"
    assert serialized["input_mode"] == input_mode
    assert serialized["message_id"].startswith("msg_")
    assert datetime.fromisoformat(serialized["created_at"]).tzinfo is not None


def test_blank_user_message_is_rejected() -> None:
    with pytest.raises(ValidationError, match="不能只包含空白字符"):
        UserMessage(text="   ")


def test_day4_contracts_serialize() -> None:
    now = datetime.now(UTC)
    segment = DialogueSegment(turn_id="turn_1", index=0, text="第一句", created_at=now)
    job = TTSJob(
        turn_id="turn_1",
        segment_id=segment.segment_id,
        text=segment.text,
        cancellation_token_id="cancel_1",
        created_at=now,
    )
    audio = AudioResult(
        job_id=job.job_id,
        turn_id=job.turn_id,
        segment_id=job.segment_id,
        success=True,
        audio_path=Path("data/cache/test.wav"),
        sample_rate=32000,
        ready_at=now,
    )
    state = TurnState(
        turn_id="turn_1",
        session_id="session_1",
        source_message_id="msg_1",
        input_mode=InputMode.voice,
        created_at=now,
        updated_at=now,
    )

    assert segment.model_dump(mode="json")["index"] == 0
    assert job.model_dump(mode="json")["timeout_ms"] == 8000
    assert audio.model_dump(mode="json")["audio_path"] == "data/cache/test.wav"
    assert state.model_dump(mode="json")["status"] == "accepted"


def test_audio_result_requires_path_on_success_and_error_on_failure() -> None:
    with pytest.raises(ValidationError, match="audio_path"):
        AudioResult(job_id="job", turn_id="turn", segment_id="segment", success=True)
    with pytest.raises(ValidationError, match="error_code"):
        AudioResult(job_id="job", turn_id="turn", segment_id="segment", success=False)
