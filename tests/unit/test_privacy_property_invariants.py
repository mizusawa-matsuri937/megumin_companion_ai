"""Hypothesis invariants for credential rejection and ephemeral-data cleanup."""

from __future__ import annotations

import asyncio
import string
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import pytest
from app.emotion.clock import FakeClock
from app.memory.models import (
    MemoryClaim,
    MemoryDecision,
    MemorySensitivity,
    MemoryType,
    SourceInputMode,
)
from app.memory.service import MemoryService
from app.perception.change_detection import FrameChangeDetector
from app.perception.guards import TextRedactor
from app.perception.models import (
    CloudAnalysis,
    ContentGuardDecision,
    GuardOutcome,
    ImageFrame,
    ObservationStatus,
    OCRResult,
    OCRSpan,
    Rect,
    SceneAnalysis,
    WindowGuardDecision,
    WindowInfo,
)
from app.perception.pipeline import PerceptionPipeline, PerceptionPipelineConfig
from app.schemas.ai import FeatureName
from app.storage.database import SQLiteDatabase
from app.storage.repositories import FeatureFlagRepository, MemoryRepository
from hypothesis import given, settings
from hypothesis import strategies as st

CredentialFamily = Literal["assignment", "token", "verification", "identity", "card"]
CredentialPlacement = Literal["source", "content", "all"]
CleanupMode = Literal["success", "error", "cancel"]

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
_ALPHANUMERIC = string.ascii_letters + string.digits + "_-"
_CREDENTIAL_FAMILIES: tuple[CredentialFamily, ...] = (
    "assignment",
    "token",
    "verification",
    "identity",
    "card",
)
_CLEANUP_MODES: tuple[CleanupMode, ...] = ("success", "error", "cancel")


@st.composite
def _assignment_credentials(draw: st.DrawFn) -> str:
    keyword = draw(
        st.sampled_from(
            ("password", "PASSCODE", "pwd", "api_key", "access-token", "secret", "密码", "密钥")
        )
    )
    separator = draw(st.sampled_from((":", " = ", "是")))
    value = draw(st.text(alphabet=_ALPHANUMERIC, min_size=4, max_size=64))
    return f"{keyword}{separator}{value}"


@st.composite
def _known_tokens(draw: st.DrawFn) -> str:
    token_kind = draw(st.sampled_from(("openai", "github", "aws", "google")))
    if token_kind == "openai":
        suffix = draw(st.text(alphabet=_ALPHANUMERIC, min_size=12, max_size=48))
        return f"sk-{suffix}"
    if token_kind == "github":
        suffix = draw(
            st.text(alphabet=string.ascii_letters + string.digits, min_size=20, max_size=48)
        )
        return f"ghp_{suffix}"
    if token_kind == "aws":
        suffix = draw(
            st.text(alphabet=string.ascii_uppercase + string.digits, min_size=16, max_size=16)
        )
        return f"AKIA{suffix}"
    suffix = draw(st.text(alphabet=_ALPHANUMERIC, min_size=20, max_size=48))
    return f"AIza{suffix}"


@st.composite
def _verification_codes(draw: st.DrawFn) -> str:
    label = draw(st.sampled_from(("验证码", "verification code", "OTP")))
    digits = draw(st.text(alphabet=string.digits, min_size=4, max_size=8))
    return f"{label}: {digits}"


@st.composite
def _identity_numbers(draw: st.DrawFn) -> str:
    prefix = draw(st.text(alphabet=string.digits, min_size=17, max_size=17))
    suffix = draw(st.sampled_from((*string.digits, "X", "x")))
    return f"身份证号 {prefix}{suffix}"


def _luhn_check_digit(prefix: list[int]) -> int:
    total = 0
    for position, digit in enumerate(reversed(prefix), start=1):
        if position % 2:
            doubled = digit * 2
            total += doubled if doubled < 10 else doubled - 9
        else:
            total += digit
    return (-total) % 10


@st.composite
def _payment_cards(draw: st.DrawFn) -> str:
    prefix = draw(st.lists(st.integers(min_value=0, max_value=9), min_size=12, max_size=18))
    digits = [*prefix, _luhn_check_digit(prefix)]
    separator = draw(st.sampled_from(("", " ", "-")))
    return f"银行卡号 {separator.join(str(digit) for digit in digits)}"


def _credential_strategy(family: CredentialFamily) -> st.SearchStrategy[str]:
    if family == "assignment":
        return _assignment_credentials()
    if family == "token":
        return _known_tokens()
    if family == "verification":
        return _verification_codes()
    if family == "identity":
        return _identity_numbers()
    return _payment_cards()


def _claim_inputs(
    credential: str,
    placement: CredentialPlacement,
) -> tuple[str, str, str]:
    ordinary = "我喜欢手冲咖啡"
    if placement == "source":
        return f"{ordinary}；{credential}", ordinary, ordinary
    if placement == "content":
        return ordinary, credential, ordinary
    return credential, credential, credential


@pytest.mark.parametrize("family", _CREDENTIAL_FAMILIES)
@settings(max_examples=12, deadline=None)
@given(
    data=st.data(),
    placement=st.sampled_from(("source", "content", "all")),
    input_mode=st.sampled_from(tuple(SourceInputMode)),
)
def test_generated_credentials_are_never_persisted_as_memory_candidates(
    family: CredentialFamily,
    data: st.DataObject,
    placement: CredentialPlacement,
    input_mode: SourceInputMode,
) -> None:
    credential = data.draw(_credential_strategy(family), label=family)
    source_text, content, evidence = _claim_inputs(credential, placement)

    with TemporaryDirectory(prefix="memory-credential-invariant-") as directory:
        database = SQLiteDatabase(Path(directory) / "memory.sqlite3")
        database.initialize()
        features = FeatureFlagRepository(database)
        features.set(FeatureName.long_term_memory, True, updated_at=_NOW)
        repository = MemoryRepository(database)
        service = MemoryService(repository, features, clock=FakeClock(_NOW))

        result = service.consider_user_claim(
            MemoryClaim(
                memory_type=MemoryType.fact,
                canonical_key="fact:credential-invariant",
                content=content,
                evidence_quote=evidence,
                importance_score=1.0,
                confidence_score=1.0,
                sensitivity_hint=MemorySensitivity.normal,
            ),
            user_id="local_user",
            source_message_id="credential-property",
            source_input_mode=input_mode,
            source_text=source_text,
            created_at=_NOW,
        )

        assert result.evaluation.decision is MemoryDecision.reject
        assert result.evaluation.reason_code == "credential_forbidden"
        assert result.item is None and result.confirmation is None
        assert service.pending_confirmations() == ()
        assert repository.list_items(user_id="local_user") == []
        assert repository.list_profiles(user_id="local_user") == []
        with database.connect() as connection:
            for table in ("memories", "memory_sources", "user_profiles", "memories_fts"):
                count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                assert count is not None and int(count[0]) == 0


class _WindowSource:
    async def active_window(self) -> WindowInfo:
        return WindowInfo("property-window", "Ordinary", "editor", Rect(0, 0, 32, 32))


class _WindowGuard:
    async def evaluate(self, window: WindowInfo) -> WindowGuardDecision:
        del window
        return WindowGuardDecision(GuardOutcome.allow, "unknown", "allowed", "fingerprint")


class _Capture:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.frames: list[ImageFrame] = []

    async def capture(self, window: WindowInfo) -> ImageFrame:
        del window
        frame = ImageFrame(bytearray(self.payload), 32, 32, perceptual_hash=42)
        self.frames.append(frame)
        return frame


class _OCR:
    def __init__(self, text: str) -> None:
        self.text = text
        self.results: list[OCRResult] = []
        self.closed = False

    async def extract(self, frame: ImageFrame) -> OCRResult:
        del frame
        result = OCRResult([OCRSpan(self.text, 1.0, Rect(1, 1, 10, 10))])
        self.results.append(result)
        return result

    async def close(self) -> None:
        self.closed = True


class _ContentGuard:
    def evaluate(self, ocr: OCRResult) -> ContentGuardDecision:
        assert ocr.spans
        return ContentGuardDecision(
            GuardOutcome.allow,
            redaction_regions=(Rect(1, 1, 10, 10),),
        )


class _Classifier:
    def classify(self, window: WindowInfo, ocr: OCRResult) -> SceneAnalysis:
        del window
        assert ocr.spans
        return SceneAnalysis("reading", "本地安全摘要", 1.0)


class _Sanitizer:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.frames: list[ImageFrame] = []
        self.closed = False

    async def sanitize(self, frame: ImageFrame, regions: tuple[Rect, ...]) -> ImageFrame:
        assert frame.data
        assert regions
        sanitized = ImageFrame(bytearray(self.payload), 16, 16)
        self.frames.append(sanitized)
        return sanitized

    async def close(self) -> None:
        self.closed = True


class _Cloud:
    def __init__(self, mode: CleanupMode) -> None:
        self.mode = mode
        self.frames: list[ImageFrame] = []
        self.started = asyncio.Event()

    async def analyze(self, frame: ImageFrame, local: SceneAnalysis) -> CloudAnalysis:
        assert local.category == "reading"
        self.frames.append(frame)
        self.started.set()
        if self.mode == "error":
            raise RuntimeError("synthetic cloud failure")
        if self.mode == "cancel":
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return CloudAnalysis("云端安全摘要", 1.0, "reading")


@pytest.mark.parametrize("mode", _CLEANUP_MODES)
@settings(max_examples=12, deadline=None)
@given(
    raw_payload=st.binary(min_size=1, max_size=512),
    sanitized_payload=st.binary(min_size=1, max_size=512),
    text_suffix=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
        min_size=0,
        max_size=64,
    ),
)
def test_perception_cleans_all_ephemeral_buffers_on_every_exit(
    mode: CleanupMode,
    raw_payload: bytes,
    sanitized_payload: bytes,
    text_suffix: str,
) -> None:
    async def scenario() -> None:
        capture = _Capture(raw_payload)
        ocr = _OCR(f"PRIVATE-OCR-{text_suffix}")
        sanitizer = _Sanitizer(sanitized_payload)
        cloud = _Cloud(mode)
        pipeline = PerceptionPipeline(
            enabled=lambda: True,
            cloud_enabled=lambda: True,
            window_source=_WindowSource(),
            window_guard=_WindowGuard(),
            capture=capture,
            change_detector=FrameChangeDetector(),
            ocr=ocr,
            content_guard=_ContentGuard(),
            classifier=_Classifier(),
            redactor=TextRedactor(),
            sanitizer=sanitizer,
            cloud=cloud,
            config=PerceptionPipelineConfig(
                operation_timeout_seconds=1.0,
                max_frame_bytes=1_024,
            ),
        )

        result = None
        try:
            observing = asyncio.create_task(pipeline.observe())
            if mode == "cancel":
                await asyncio.wait_for(cloud.started.wait(), timeout=1.0)
                observing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await observing
            else:
                result = await observing
        finally:
            await pipeline.close()

        if mode == "success":
            assert result is not None and result.status is ObservationStatus.analyzed_cloud
        elif mode == "error":
            assert result is not None and result.status is ObservationStatus.cloud_error
        else:
            assert result is None
        assert capture.frames and all(frame.data == bytearray() for frame in capture.frames)
        assert ocr.results and all(item.spans == [] for item in ocr.results)
        assert sanitizer.frames and all(frame.data == bytearray() for frame in sanitizer.frames)
        assert cloud.frames == sanitizer.frames
        assert ocr.closed and sanitizer.closed

    asyncio.run(scenario())
