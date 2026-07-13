"""Pure privacy guard, classifier, change detector, and limiter tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.perception.change_detection import FrameChangeDetector
from app.perception.classification import LocalSceneClassifier
from app.perception.guards import OCRContentGuard, PreCaptureGuard, TextRedactor
from app.perception.models import (
    GuardOutcome,
    ImageFrame,
    OCRResult,
    OCRSpan,
    Rect,
    SceneAnalysis,
    WindowInfo,
)
from app.perception.rate_limit import SlidingWindowRateLimiter


def _window(
    *,
    window_id: str = "window-1",
    title: str = "Ordinary document",
    process: str = "ordinary.exe",
) -> WindowInfo:
    return WindowInfo(
        window_id=window_id,
        title=title,
        process_name=process,
        rect=Rect(10, 20, 800, 600),
    )


def _frame(data: bytes, *, perceptual_hash: int | None = None) -> ImageFrame:
    return ImageFrame(
        data=bytearray(data),
        width=20,
        height=10,
        perceptual_hash=perceptual_hash,
    )


@pytest.mark.parametrize(
    ("title", "process", "reason"),
    [
        ("Notes", "Bitwarden.exe", "sensitive_process"),
        ("BANK CHECKOUT", "browser.exe", "sensitive_title"),
        ("输入验证码", "browser.exe", "sensitive_title"),
        ("Private Browsing", "Safari", "sensitive_title"),
    ],
)
def test_pre_capture_guard_blocks_before_screenshot_and_only_exposes_fingerprint(
    title: str, process: str, reason: str
) -> None:
    async def scenario() -> None:
        decision = await PreCaptureGuard().evaluate(_window(title=title, process=process))
        assert decision.outcome is GuardOutcome.block
        assert decision.reason_code == reason
        assert decision.category == "sensitive"
        assert process.casefold() not in decision.process_fingerprint
        assert len(decision.process_fingerprint) == 16

    import asyncio

    asyncio.run(scenario())


def test_unknown_window_is_allowed_after_successful_guard_check() -> None:
    async def scenario() -> None:
        decision = await PreCaptureGuard().evaluate(_window())
        assert decision.outcome is GuardOutcome.allow
        assert decision.reason_code == "guard_passed"
        assert decision.category == "unknown"

    import asyncio

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("contact fake.person@example.test", "email"),
        ("mobile 13800138000", "phone"),
        ("token sk-test_123456789012345", "api_key"),
        ("password: not-a-real-password", "credential_context"),
        ("card 4111 1111 1111 1111", "long_number"),
        ("我的身份证信息", "sensitive_keyword"),
    ],
)
def test_content_guard_blocks_synthetic_sensitive_ocr(text: str, reason: str) -> None:
    box = Rect(1, 2, 30, 12)
    result = OCRResult(spans=[OCRSpan(text=text, confidence=0.9, box=box)])
    decision = OCRContentGuard().evaluate(result)
    assert decision.outcome is GuardOutcome.block
    assert reason in decision.reason_codes
    if reason != "sensitive_keyword":
        assert decision.redaction_regions == (box,)


def test_content_guard_allows_checked_ordinary_text_and_limits_scan() -> None:
    guard = OCRContentGuard(max_text_chars=12)
    result = OCRResult(spans=[OCRSpan(text="ordinary text then fake@example.test", confidence=0.7)])
    decision = guard.evaluate(result)
    assert decision.outcome is GuardOutcome.allow
    result.wipe()
    assert result.text == ""


def test_text_redactor_removes_pii_controls_and_bounds_summary() -> None:
    redactor = TextRedactor(max_chars=80)
    summary = redactor.redact(
        "mail fake.person@example.test\nphone 13800138000\x00 key sk-test_123456789012345"
    )
    assert "fake.person" not in summary
    assert "13800138000" not in summary
    assert "123456789012345" not in summary
    assert "\x00" not in summary
    assert "[EMAIL]" in summary and "[PHONE]" in summary and "[SECRET]" in summary
    assert len(summary) <= 80
    assert TextRedactor().redact("\x00\n") == "屏幕场景已完成隐私处理。"
    with pytest.raises(ValueError):
        TextRedactor(max_chars=0)


def test_change_detector_uses_window_identity_perceptual_hash_and_digest() -> None:
    detector = FrameChangeDetector(minimum_hash_distance=0.1)
    window = _window()
    assert detector.should_analyze(window, _frame(b"first", perceptual_hash=0))
    assert not detector.should_analyze(window, _frame(b"other bytes", perceptual_hash=1))
    assert detector.should_analyze(window, _frame(b"other bytes", perceptual_hash=(1 << 10) - 1))

    second_window = _window(window_id="window-2")
    assert detector.should_analyze(second_window, _frame(b"same"))
    assert not detector.should_analyze(second_window, _frame(b"same"))
    assert detector.should_analyze(second_window, _frame(b"changed"))
    detector.reset()
    assert detector.should_analyze(second_window, _frame(b"changed"))
    with pytest.raises(ValueError):
        FrameChangeDetector(minimum_hash_distance=1.1)
    with pytest.raises(ValueError):
        FrameChangeDetector(max_windows=0)

    bounded = FrameChangeDetector(max_windows=1)
    assert bounded.should_analyze(window, _frame(b"same"))
    assert bounded.should_analyze(second_window, _frame(b"same"))
    assert bounded.should_analyze(window, _frame(b"same"))


def test_sliding_window_limiter_has_deterministic_boundary() -> None:
    now = [100.0]
    limiter = SlidingWindowRateLimiter(
        max_calls=2,
        period_seconds=10,
        clock=lambda: now[0],
    )
    assert limiter.allow()
    assert limiter.allow()
    assert not limiter.allow()
    now[0] = 110.0
    assert limiter.allow()
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_calls=0, period_seconds=1)


@pytest.mark.parametrize(
    ("process", "text", "category"),
    [
        ("Code.exe", "def example", "coding"),
        ("Resolve.exe", "render timeline", "editing"),
        ("Steam.exe", "match fps", "game"),
        ("Safari", "documentation article", "reading"),
        ("Calculator", "2 + 2", "unknown"),
    ],
)
def test_scene_classifier_returns_only_coarse_safe_summary(
    process: str, text: str, category: str
) -> None:
    analysis = LocalSceneClassifier().classify(
        _window(process=process),
        OCRResult(spans=[OCRSpan(text=text, confidence=0.8)]),
    )
    assert analysis.category == category
    assert text not in analysis.summary
    assert 0 <= analysis.confidence <= 1


def test_internal_model_validation_and_frame_wipe() -> None:
    with pytest.raises(ValueError):
        Rect(0, 0, 0, 2)
    with pytest.raises(ValueError):
        WindowInfo("", "title", "process", Rect(0, 0, 1, 1))
    with pytest.raises(ValueError):
        OCRSpan("x", 1.1)
    with pytest.raises(ValueError):
        SceneAnalysis("x", " ", 0.5)
    with pytest.raises(ValueError):
        SceneAnalysis("x", "ok", -0.1)
    with pytest.raises(ValueError):
        ImageFrame(bytearray(), 1, 1)
    with pytest.raises(ValueError):
        ImageFrame(bytearray(b"x"), 0, 1)
    with pytest.raises(ValueError):
        ImageFrame(bytearray(b"x"), 1, 1, content_type="image/gif")

    frame = _frame(b"private pixels", perceptual_hash=123)
    retained_data = frame.data
    frame.wipe()
    assert retained_data == bytearray()
    assert frame.perceptual_hash is None


def test_no_fixture_contains_real_screen_material() -> None:
    # Keep a simple repository-level invariant beside the synthetic fixture tests.
    fixture_root = Path(__file__).parents[1] / "fixtures"
    assert not list(fixture_root.glob("**/*screen*")) if fixture_root.exists() else True
