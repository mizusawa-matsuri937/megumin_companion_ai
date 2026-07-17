"""Cross-module privacy sentinel, arbitration, failure-storm, and shutdown evidence."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from app.clients.llm import OpenAICompatibleLLMProvider
from app.clients.tts import MockTTSProvider
from app.config import Settings
from app.config.logging import configure_logging
from app.config.settings import LoggingConfig
from app.core import CancellationToken, TurnService
from app.emotion import FakeClock
from app.memory.runtime import create_memory_runtime
from app.paths import AppPaths
from app.perception.change_detection import FrameChangeDetector
from app.perception.classification import LocalSceneClassifier
from app.perception.guards import OCRContentGuard, PreCaptureGuard, TextRedactor
from app.perception.models import (
    ImageFrame,
    ObservationStatus,
    OCRResult,
    OCRSpan,
    Rect,
    WindowInfo,
)
from app.perception.pipeline import PerceptionPipeline
from app.pipelines import DialoguePipeline
from app.pipelines.audio_player import SilentAudioPlayer
from app.proactive import (
    ProactivePolicy,
    ProactiveRuntime,
    ProactiveSuppression,
    ProactiveTrigger,
    ProactiveTriggerType,
)
from app.schemas import (
    FeatureName,
    FeatureState,
    PipelineEvent,
    ProactiveIntent,
    TurnMetrics,
    TurnOutcome,
    TurnState,
    UserMessage,
)

NOW = datetime(2026, 7, 13, 4, 0, tzinfo=UTC)
PRIVACY_SENTINEL = "SCREEN_PRIVACY_SENTINEL password=screen-only-secret-937"


class _WindowSource:
    async def active_window(self) -> WindowInfo:
        return WindowInfo("ordinary-window", "Ordinary Editor", "Code", Rect(0, 0, 64, 32))


class _Capture:
    def __init__(self) -> None:
        self.frames: list[ImageFrame] = []

    async def capture(self, _window: WindowInfo) -> ImageFrame:
        frame = ImageFrame(bytearray(b"synthetic-frame"), 64, 32, perceptual_hash=42)
        self.frames.append(frame)
        return frame


class _SentinelOCR:
    def __init__(self) -> None:
        self.results: list[OCRResult] = []
        self.close_calls = 0

    async def extract(self, _frame: ImageFrame) -> OCRResult:
        result = OCRResult(spans=[OCRSpan(PRIVACY_SENTINEL, 0.99, Rect(1, 1, 30, 5))])
        self.results.append(result)
        return result

    async def close(self) -> None:
        self.close_calls += 1


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.close_calls = 0

    def publish(self, event: PipelineEvent) -> bool:
        self.events.append(event.model_dump_json())
        return True

    async def close(self) -> None:
        self.close_calls += 1


class _StaticFeatures:
    def __init__(self) -> None:
        self.listeners: set[Callable[[FeatureState], None]] = set()

    def get_feature(self, name: FeatureName) -> FeatureState:
        return FeatureState(name=name, enabled=name is FeatureName.proactive)

    def subscribe(self, listener: Callable[[FeatureState], None]) -> Callable[[], None]:
        self.listeners.add(listener)

        def unsubscribe() -> None:
            self.listeners.discard(listener)

        return unsubscribe


class _FailingObserver:
    async def on_user_accepted(self, _message: UserMessage, _state: TurnState) -> None:
        raise RuntimeError("OBSERVER_PRIVATE_SENTINEL")

    async def on_turn_completed(
        self,
        _message: UserMessage,
        _state: TurnState,
        _outcome: TurnOutcome,
    ) -> None:
        raise RuntimeError("OBSERVER_PRIVATE_SENTINEL")


class _FailingSink:
    def __init__(self) -> None:
        self.publish_calls = 0
        self.close_calls = 0

    def publish(self, _event: PipelineEvent) -> bool:
        self.publish_calls += 1
        raise RuntimeError("SINK_PRIVATE_SENTINEL")

    async def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("SINK_CLOSE_PRIVATE_SENTINEL")


class _StormPipeline:
    def __init__(self) -> None:
        self.proactive_started = asyncio.Event()
        self.user_started = asyncio.Event()
        self.active_runs = 0
        self.max_active_runs = 0
        self.proactive_cancelled = 0
        self.user_cancelled = 0
        self.close_calls = 0

    async def run(
        self,
        message: UserMessage,
        _state: TurnState,
        _token: CancellationToken,
        _emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        self._enter()
        try:
            if message.text == "fail-now":
                await asyncio.sleep(0)
                raise RuntimeError("PIPELINE_PRIVATE_SENTINEL")
            self.user_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.user_cancelled += 1
                raise
            raise AssertionError("unreachable")
        finally:
            self.active_runs -= 1

    async def run_proactive(
        self,
        _intent: ProactiveIntent,
        _state: TurnState,
        _token: CancellationToken,
        _emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        self._enter()
        self.proactive_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.proactive_cancelled += 1
            raise
        finally:
            self.active_runs -= 1
        raise AssertionError("unreachable")

    def _enter(self) -> None:
        self.active_runs += 1
        self.max_active_runs = max(self.max_active_runs, self.active_runs)

    async def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("PIPELINE_CLOSE_PRIVATE_SENTINEL")


class _SchedulerPipeline:
    def __init__(self) -> None:
        self.completed = asyncio.Event()
        self.proactive_calls = 0
        self.close_calls = 0

    async def run(
        self,
        _message: UserMessage,
        _state: TurnState,
        _token: CancellationToken,
        _emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        raise AssertionError("scheduler E2E must not create a user turn")

    async def run_proactive(
        self,
        _intent: ProactiveIntent,
        _state: TurnState,
        token: CancellationToken,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TurnOutcome:
        token.raise_if_cancelled()
        self.proactive_calls += 1
        await emit("assistant.delta", {"delta": "固定主动测试回复"})
        self.completed.set()
        return TurnOutcome(
            full_text="固定主动测试回复",
            metrics=TurnMetrics(turn_total_ms=1),
        )

    async def close(self) -> None:
        self.close_calls += 1


def _logger(log_path: Path) -> logging.Logger:
    settings = Settings(
        logging=LoggingConfig(
            console_enabled=False,
            file_enabled=True,
            file_path=Path(log_path.name),
        )
    )
    settings._paths = AppPaths(root=log_path.parent.parent)
    return configure_logging(settings)


def _policy() -> ProactivePolicy:
    return ProactivePolicy(
        minimum_score=0,
        cooldown=timedelta(0),
        idle_minimum=timedelta(0),
        quiet_start_hour=0,
        quiet_end_hour=0,
    )


def _trigger(trigger_type: ProactiveTriggerType, instruction: str) -> ProactiveTrigger:
    return ProactiveTrigger(
        trigger_type=trigger_type,
        instruction=instruction,
        confidence=1,
        novelty=1,
        urgency=1,
        voice_allowed=False,
        observed_at=NOW,
    )


def _pending_backend_tasks() -> list[str]:
    prefixes = (
        "dialogue-",
        "proactive-",
        "turn-post-commit-",
        "turn-service-",
        "memory-",
        "vts-",
        "gpt-sovits-",
        "local-stt-",
        "voice-",
        "whisper-",
        "rapidocr-",
        "pillow-",
        "perception-",
    )
    current = asyncio.current_task()
    return [
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not current and not task.done() and task.get_name().startswith(prefixes)
    ]


def test_privacy_sentinel_never_crosses_persistence_event_network_or_file_boundaries(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        log_path = tmp_path / "logs" / "app.jsonl"
        database_path = tmp_path / "private" / "companion.sqlite3"
        cache_path = tmp_path / "cache"
        temporary_path = tmp_path / "temporary"
        cache_path.mkdir()
        temporary_path.mkdir()
        logger = _logger(log_path)

        memory = await create_memory_runtime(str(database_path))
        await memory.set_feature(FeatureName.proactive, True)
        await memory.set_feature(FeatureName.vision, True)
        proactive = ProactiveRuntime(memory.features, _policy(), clock=FakeClock(NOW))
        memory.add_feature_transition_handler(proactive.apply_feature_state)

        network_records: list[bytes] = []

        def llm_handler(request: httpx.Request) -> httpx.Response:
            network_records.append(bytes(request.content))
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"content":"主动后端检查完成。"}}]}\n\n'
                    "data: [DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )

        http_client = httpx.AsyncClient(
            base_url="https://provider.invalid",
            transport=httpx.MockTransport(llm_handler),
        )
        llm = OpenAICompatibleLLMProvider(
            base_url="https://provider.invalid",
            model="test-model",
            api_key="fake-test-key",
            client=http_client,
        )
        sink = _RecordingSink()
        pipeline = DialoguePipeline(
            llm,
            MockTTSProvider(cache_path, duration_ms=0),
            SilentAudioPlayer(),
        )
        service = TurnService(
            logger,
            pipeline,
            observers=(memory.observer,),
            event_sinks=(sink,),
            priority_controller=proactive,
        )

        capture = _Capture()
        ocr = _SentinelOCR()
        perception = PerceptionPipeline(
            enabled=lambda: True,
            window_source=_WindowSource(),
            window_guard=PreCaptureGuard(),
            capture=capture,
            change_detector=FrameChangeDetector(),
            ocr=ocr,
            content_guard=OCRContentGuard(),
            classifier=LocalSceneClassifier(),
            redactor=TextRedactor(),
        )

        observation = await perception.observe()
        assert observation.status is ObservationStatus.blocked_after_ocr
        assert observation.context is not None and observation.context.sensitive
        assert PRIVACY_SENTINEL not in observation.context.model_dump_json()
        assert capture.frames and capture.frames[0].data == bytearray()
        assert ocr.results and ocr.results[0].spans == []

        allowed = await proactive.submit(
            _trigger(ProactiveTriggerType.task_complete, PRIVACY_SENTINEL),
            service.run_proactive,
        )
        assert allowed.intent is not None
        assert PRIVACY_SENTINEL not in allowed.intent.model_dump_json()
        await proactive.wait_idle()
        assert len(network_records) == 1

        await proactive.update_perception(observation.context)
        blocked = await proactive.submit(
            _trigger(ProactiveTriggerType.visual_change, PRIVACY_SENTINEL),
            service.run_proactive,
        )
        assert blocked.suppression is ProactiveSuppression.sensitive
        assert len(network_records) == 1

        await proactive.close()
        await service.shutdown()
        await perception.close()
        await memory.close()
        await http_client.aclose()
        for handler in logger.handlers:
            handler.flush()

        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone() == (
                0,
            )
            assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (0,)

        event_records = "\n".join(sink.events).encode()
        network_payload = b"\n".join(network_records)
        file_payload = b"\n".join(
            path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
        )
        sentinel_bytes = PRIVACY_SENTINEL.encode()
        assert sentinel_bytes not in log_path.read_bytes()
        assert sentinel_bytes not in database_path.read_bytes()
        assert sentinel_bytes not in event_records
        assert sentinel_bytes not in network_payload
        assert sentinel_bytes not in file_payload
        assert not list(cache_path.rglob("*.wav"))
        assert list(temporary_path.iterdir()) == []
        assert sink.close_calls == 1
        assert ocr.close_calls == 1
        assert _pending_backend_tasks() == []

    asyncio.run(scenario())


def test_failure_storm_preserves_user_priority_and_all_settled_shutdown(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        log_path = tmp_path / "logs" / "failure-storm.jsonl"
        logger = _logger(log_path)
        features = _StaticFeatures()
        proactive = ProactiveRuntime(features, _policy(), clock=FakeClock(NOW))
        pipeline = _StormPipeline()
        sink = _FailingSink()
        service = TurnService(
            logger,
            pipeline,
            observers=(_FailingObserver(),),
            event_sinks=(sink,),
            priority_controller=proactive,
        )
        events = service.subscribe("*")

        decision = await proactive.submit(
            _trigger(ProactiveTriggerType.task_complete, "start background"),
            service.run_proactive,
        )
        assert decision.intent is not None
        await pipeline.proactive_started.wait()

        first = await service.accept(UserMessage(text="first-user-turn"))
        await pipeline.user_started.wait()
        assert pipeline.proactive_cancelled == 1

        failed = await service.accept(UserMessage(text="fail-now"))
        while True:
            event = await asyncio.wait_for(events.get(), timeout=1)
            events.task_done()
            if event.type == "turn.failed" and event.turn_id == failed.turn_id:
                break

        assert pipeline.user_cancelled == 1
        assert pipeline.max_active_runs == 1
        assert service.snapshot()["turns"][first.turn_id]["status"] == "cancelled"
        assert service.snapshot()["turns"][failed.turn_id]["status"] == "failed"

        await proactive.close()
        shutdown_results = await asyncio.gather(
            service.shutdown(),
            service.shutdown(),
            service.shutdown(),
            return_exceptions=True,
        )
        assert all(result is None for result in shutdown_results)
        assert pipeline.close_calls == 1
        assert sink.close_calls == 1
        assert sink.publish_calls >= 4
        assert service.snapshot()["active_turns"] == []
        assert _pending_backend_tasks() == []

        for handler in logger.handlers:
            handler.flush()
        logs = log_path.read_text(encoding="utf-8")
        for private_error in (
            "OBSERVER_PRIVATE_SENTINEL",
            "SINK_PRIVATE_SENTINEL",
            "SINK_CLOSE_PRIVATE_SENTINEL",
            "PIPELINE_PRIVATE_SENTINEL",
            "PIPELINE_CLOSE_PRIVATE_SENTINEL",
        ):
            assert private_error not in logs

    asyncio.run(scenario())


def test_scheduler_to_turn_service_never_persists_as_user_or_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "scheduler.sqlite3"
        memory = await create_memory_runtime(str(database_path))
        await memory.set_feature(FeatureName.proactive, True)
        proactive = ProactiveRuntime(
            memory.features,
            ProactivePolicy(
                minimum_score=0,
                cooldown=timedelta(hours=1),
                idle_minimum=timedelta(0),
                quiet_start_hour=0,
                quiet_end_hour=0,
            ),
            clock=FakeClock(NOW),
        )
        memory.add_feature_transition_handler(proactive.apply_feature_state)
        pipeline = _SchedulerPipeline()
        sink = _RecordingSink()
        service = TurnService(
            logging.getLogger("test.scheduler-e2e"),
            pipeline,
            observers=(memory.observer,),
            event_sinks=(sink,),
            priority_controller=proactive,
        )

        assert proactive.start(service.run_proactive, interval=timedelta(milliseconds=2))
        await asyncio.wait_for(pipeline.completed.wait(), timeout=1)
        await proactive.wait_idle()
        disabled = await memory.set_feature(FeatureName.proactive, False)
        assert not disabled.enabled
        await proactive.close()
        await service.shutdown()
        await memory.close()

        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone() == (
                0,
            )
            assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (0,)
        assert pipeline.proactive_calls == 1
        assert pipeline.close_calls == 1
        assert any("proactive.completed" in event for event in sink.events)
        assert _pending_backend_tasks() == []

    asyncio.run(scenario())
