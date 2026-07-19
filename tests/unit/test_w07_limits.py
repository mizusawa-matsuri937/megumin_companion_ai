from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from app.api.protocol import DevAPIProtocolError, validate_metadata
from app.api.security import DevAPIConfig
from app.config.settings import LLMConfig, Settings, TTSConfig
from app.emotion.models import EmotionLabel, EmotionState
from app.limits import LimitsConfig
from app.pipelines.dialogue import _AudioByteBudget, _MeasuredQueue, _utf8_prefix
from app.prompts.builder import PromptBuilder
from app.prompts.models import PromptBudget
from app.prompts.tokens import ProviderTokenEstimator
from hypothesis import given
from hypothesis import strategies as st


def _emotion() -> EmotionState:
    now = datetime(2026, 7, 18, tzinfo=UTC)
    return EmotionState(
        dominant_label=EmotionLabel.focused,
        intensity=0.2,
        last_updated_at=now,
        label_since=now,
    )


def _dev_api(**overrides: object) -> DevAPIConfig:
    values: dict[str, object] = {
        "token": "w07-test-token-000000000000000000000000",
        "client_id": "w07-client",
        "session_id": "w07-session",
        "allowed_origins": frozenset({"http://127.0.0.1:8765"}),
        "allowed_hosts": frozenset({"127.0.0.1:8765"}),
    }
    values.update(overrides)
    return DevAPIConfig(**values)  # type: ignore[arg-type]


def test_limits_defaults_are_the_approved_w07_hard_caps() -> None:
    limits = LimitsConfig()

    assert limits.dev_http_body_bytes == 64 * 1024
    assert limits.dev_websocket_frame_bytes == 64 * 1024
    assert limits.metadata_bytes == 8 * 1024
    assert (limits.metadata_keys, limits.metadata_depth) == (16, 3)
    assert (limits.tts_queue_capacity, limits.ready_audio_queue_capacity) == (8, 4)
    assert limits.audio_inflight_bytes == 64 * 1024 * 1024
    assert (limits.llm_output_bytes, limits.llm_output_segments) == (64 * 1024, 128)


def test_limits_reject_internal_audio_and_prompt_contradictions() -> None:
    with pytest.raises(ValueError, match="single-result"):
        LimitsConfig(audio_single_result_bytes=1024, audio_inflight_bytes=512)
    with pytest.raises(ValueError, match="current-user"):
        LimitsConfig(prompt_total_tokens=256, prompt_system_tokens=256)


@pytest.mark.parametrize(
    "settings",
    [
        Settings(
            llm=LLMConfig(max_tokens=4_097),
            limits=LimitsConfig(provider_output_tokens=4_096),
        ),
        Settings(
            tts=TTSConfig(max_audio_bytes=65 * 1024 * 1024),
            limits=LimitsConfig(audio_inflight_bytes=64 * 1024 * 1024),
        ),
    ],
)
def test_startup_self_check_rejects_provider_limits_that_bypass_hard_caps(
    settings: Settings,
) -> None:
    with pytest.raises(ValueError, match="hard limit"):
        settings.validate_runtime_limits()


def test_metadata_has_independent_utf8_byte_key_and_depth_limits() -> None:
    config = _dev_api(max_metadata_bytes=32, max_metadata_keys=2, max_metadata_depth=2)
    validate_metadata({"a": "ok", "b": "好"}, config)

    for metadata in (
        {"a": "🙂" * 20},
        {"a": 1, "b": 2, "c": 3},
        {"a": {"b": {"c": 1}}},
    ):
        with pytest.raises(DevAPIProtocolError, match="metadata_limits_exceeded"):
            validate_metadata(metadata, config)

    assert len(json.dumps({"a": "🙂" * 20}, ensure_ascii=False).encode("utf-8")) > 32


def test_provider_token_estimator_is_model_aware_and_conservative_for_mixed_text() -> None:
    text = "ASCII words 中文 日本語 🙂🙂"
    openai = ProviderTokenEstimator(provider="openai", model="gpt-4.1")
    mock = ProviderTokenEstimator(provider="mock", model="")

    assert openai.profile == "openai_cl100k_conservative"
    assert mock.profile == "unicode_conservative"
    assert openai.estimate(text) > 0
    truncated = openai.truncate(text * 100, 12)
    assert truncated.endswith("…")
    assert openai.estimate(truncated) <= 12
    assert "\ufffd" not in truncated
    assert openai.estimate("") == 0
    assert openai.truncate("ok", 10) == "ok"
    assert openai.truncate("🙂", 0) == ""
    assert openai.truncate("🙂", 1) == "."


def test_prompt_total_budget_can_only_degrade_current_after_other_sources() -> None:
    baseline_builder = PromptBuilder()
    baseline = baseline_builder.build_with_report(current_user_text="x", emotion=_emotion())
    constrained = PromptBuilder(
        budget=PromptBudget(
            total_tokens=baseline.estimated_prompt_tokens,
            current_user_tokens=100,
        )
    ).build_with_report(current_user_text="x" * 100, emotion=_emotion())

    assert constrained.degradation_steps == ("current_user",)
    assert constrained.current_user_truncated
    assert constrained.estimated_prompt_tokens <= baseline.estimated_prompt_tokens


def test_bounded_queue_and_weighted_audio_budget_close_and_release_to_zero() -> None:
    async def scenario() -> None:
        queue = _MeasuredQueue[int](2)
        await queue.put(1)
        await queue.close(1)
        assert await queue.get() == 1
        queue.task_done()
        assert not isinstance(await queue.get(), int)
        queue.task_done()
        await queue.close(1)
        assert queue.drain() == []
        assert queue.report().final_depth == 0

        budget = _AudioByteBudget(4)
        with pytest.raises(ValueError, match="reservation"):
            await budget.acquire(5)
        await budget.acquire(4)
        with pytest.raises(ValueError, match="reserved"):
            await budget.shrink(4, 5)
        await budget.shrink(4, 2)
        await budget.release(0)
        await budget.release(2)
        assert budget.used == 0

    asyncio.run(scenario())


@given(st.text(max_size=256), st.integers(min_value=0, max_value=256))
def test_utf8_prefix_property_never_splits_a_codepoint(value: str, byte_limit: int) -> None:
    prefix, cut = _utf8_prefix(value, byte_limit)

    assert len(prefix.encode("utf-8")) <= byte_limit
    assert "\ufffd" not in prefix
    assert value.startswith(prefix)
    assert cut is (len(value.encode("utf-8")) > byte_limit)
