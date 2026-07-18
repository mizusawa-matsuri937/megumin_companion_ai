from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from app.clients.tts import MockTTSProvider
from app.core import CancellationToken
from app.limits import LimitsConfig
from app.pipelines import DialoguePipeline
from app.pipelines.audio_player import SilentAudioPlayer
from app.pipelines.dialogue import PipelineResourceReport
from app.schemas import (
    AudioResult,
    ChatCompletion,
    ChatRequest,
    TTSJob,
    TurnOutcome,
    TurnState,
    UserMessage,
)


class _BurstLLM:
    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas

    async def stream(self, request: ChatRequest, token: CancellationToken) -> AsyncIterator[str]:
        del request
        for delta in self._deltas:
            token.raise_if_cancelled()
            yield delta

    async def complete(self, request: ChatRequest, token: CancellationToken) -> ChatCompletion:
        del request
        token.raise_if_cancelled()
        return ChatCompletion(text="".join(self._deltas))

    async def close(self) -> None:
        return None


class _DiskFullTTS(MockTTSProvider):
    async def synthesize(
        self, job: TTSJob, *, segment_index: int, token: CancellationToken
    ) -> AudioResult:
        del job, segment_index
        token.raise_if_cancelled()
        raise OSError("private disk-full path must not escape")


class _BlockedAudioPlayer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop_calls = 0

    async def play(self, result: AudioResult, token: CancellationToken) -> None:
        del result
        token.raise_if_cancelled()
        self.started.set()
        await asyncio.Event().wait()

    async def stop(self, *, immediate: bool = False) -> None:
        del immediate
        self.stop_calls += 1

    async def close(self) -> None:
        return None


async def _run(
    pipeline: DialoguePipeline,
    *,
    cancel_after: float | None = None,
) -> tuple[TurnOutcome | BaseException, list[tuple[str, dict[str, object]]]]:
    message = UserMessage(text="W07 bounded pipeline")
    state = TurnState(
        session_id=message.session_id,
        source_message_id=message.message_id,
        input_mode=message.input_mode,
    )
    token = CancellationToken(state.turn_id)
    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    task = asyncio.create_task(pipeline.run(message, state, token, emit))
    if cancel_after is not None:
        await asyncio.sleep(cancel_after)
        token.cancel()
        task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)
    return result[0], events


def test_fast_llm_slow_tts_and_blocked_audio_produce_measured_backpressure(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[dict[str, object], PipelineResourceReport]:
        limits = LimitsConfig(
            tts_queue_capacity=2,
            ready_audio_queue_capacity=1,
            audio_single_result_bytes=128 * 1024,
            audio_inflight_bytes=256 * 1024,
        )
        pipeline = DialoguePipeline(
            _BurstLLM([f"第{i}段已经完成。" for i in range(20)]),
            MockTTSProvider(
                tmp_path,
                duration_ms=250,
                synthesis_delay_seconds=0.01,
            ),
            SilentAudioPlayer(realtime=True),
            limits=limits,
            tts_worker_count=2,
        )
        result, _events = await _run(pipeline)
        report = pipeline.last_resource_report
        await pipeline.close()
        if isinstance(result, BaseException):
            raise AssertionError("bounded pipeline unexpectedly failed") from result
        assert report is not None
        return result.metrics.model_dump(mode="json"), report

    metrics, report = asyncio.run(scenario())

    assert report.tts_queue.capacity == 2
    assert report.tts_queue.max_depth <= 2
    assert report.tts_queue.final_depth == 0
    assert report.ready_audio_queue.capacity == 1
    assert report.ready_audio_queue.max_depth <= 1
    assert report.ready_audio_queue.final_depth == 0
    assert report.tts_queue.producer_block_count > 0
    assert report.max_audio_inflight_bytes <= 256 * 1024
    assert report.final_audio_inflight_bytes == 0
    assert metrics["segment_count"] == 20
    assert not list(tmp_path.rglob("*.wav"))


def test_utf8_and_segment_limits_end_in_explainable_truncation(tmp_path: Path) -> None:
    async def scenario() -> tuple[TurnOutcome, list[tuple[str, dict[str, object]]]]:
        pipeline = DialoguePipeline(
            _BurstLLM(["中🙂日A。" * 20]),
            MockTTSProvider(tmp_path, duration_ms=0, synthesis_delay_seconds=0),
            SilentAudioPlayer(),
            limits=LimitsConfig(llm_output_bytes=31, llm_output_segments=2),
        )
        result, events = await _run(pipeline)
        await pipeline.close()
        if isinstance(result, BaseException):
            raise AssertionError("truncated pipeline unexpectedly failed") from result
        return result, events

    result, events = asyncio.run(scenario())

    assert len(result.full_text.encode("utf-8")) <= 31
    assert len(result.segments) <= 2
    assert result.metadata["terminal"] == "truncated"
    truncated = [payload for name, payload in events if name == "assistant.truncated"]
    assert truncated and truncated[-1]["reason"] in {"output_bytes", "output_segments"}
    assert "\ufffd" not in result.full_text


def test_cancellation_storm_drains_queues_releases_leases_and_removes_temp(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[object]:
        results: list[object] = []
        for _ in range(25):
            pipeline = DialoguePipeline(
                _BurstLLM([f"第{i}段取消测试。" for i in range(50)]),
                MockTTSProvider(
                    tmp_path,
                    duration_ms=100,
                    synthesis_delay_seconds=0.01,
                ),
                SilentAudioPlayer(realtime=True),
                limits=LimitsConfig(
                    tts_queue_capacity=2,
                    ready_audio_queue_capacity=1,
                    audio_single_result_bytes=128 * 1024,
                    audio_inflight_bytes=256 * 1024,
                ),
            )
            result, _events = await _run(pipeline, cancel_after=0.015)
            results.append(result)
            report = pipeline.last_resource_report
            assert report is not None
            assert report.cleanup_ms >= 0
            assert report.max_audio_inflight_bytes <= 256 * 1024
            assert report.final_audio_inflight_bytes == 0
            assert report.tts_queue.final_depth == 0
            assert report.ready_audio_queue.final_depth == 0
            await pipeline.close()
        return results

    results = asyncio.run(scenario())

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not list(tmp_path.rglob("*.wav"))


def test_blocked_audio_device_backpressures_then_cancels_to_stable_empty_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[BaseException, PipelineResourceReport, int]:
        player = _BlockedAudioPlayer()
        pipeline = DialoguePipeline(
            _BurstLLM([f"第{i}段声卡阻塞测试。" for i in range(50)]),
            MockTTSProvider(tmp_path, duration_ms=10, synthesis_delay_seconds=0),
            player,
            limits=LimitsConfig(
                tts_queue_capacity=2,
                ready_audio_queue_capacity=1,
                audio_single_result_bytes=128 * 1024,
                audio_inflight_bytes=256 * 1024,
            ),
        )
        result, _events = await _run(pipeline, cancel_after=0.05)
        report = pipeline.last_resource_report
        await pipeline.close()
        assert isinstance(result, BaseException)
        assert report is not None
        return result, report, player.stop_calls

    result, report, stop_calls = asyncio.run(scenario())

    assert isinstance(result, asyncio.CancelledError)
    assert report.tts_queue.producer_block_count > 0
    assert report.tts_queue.final_depth == 0
    assert report.ready_audio_queue.final_depth == 0
    assert report.final_audio_inflight_bytes == 0
    assert stop_calls == 1
    assert not list(tmp_path.rglob("*.wav"))


def test_total_audio_duration_limit_degrades_without_leaving_temp(tmp_path: Path) -> None:
    async def scenario() -> tuple[TurnOutcome, list[tuple[str, dict[str, object]]]]:
        pipeline = DialoguePipeline(
            _BurstLLM(["第一段完成。", "第二段完成。"]),
            MockTTSProvider(tmp_path, duration_ms=100, synthesis_delay_seconds=0),
            SilentAudioPlayer(),
            limits=LimitsConfig(audio_total_duration_ms=1),
        )
        result, events = await _run(pipeline)
        await pipeline.close()
        if isinstance(result, BaseException):
            raise AssertionError("audio degradation unexpectedly failed the turn") from result
        return result, events

    result, events = asyncio.run(scenario())

    assert result.metadata["terminal"] == "completed"
    degraded = [payload for name, payload in events if name == "audio.degraded"]
    assert degraded and all(item["reason"] == "audio_duration_limit" for item in degraded)
    assert result.metrics.playback_count == 0
    assert not list(tmp_path.rglob("*.wav"))


def test_disk_failure_has_stable_failed_report_and_no_temp_leak(tmp_path: Path) -> None:
    async def scenario() -> tuple[BaseException, PipelineResourceReport]:
        pipeline = DialoguePipeline(
            _BurstLLM(["磁盘故障测试完成。"]),
            _DiskFullTTS(tmp_path),
            SilentAudioPlayer(),
        )
        result, _events = await _run(pipeline)
        report = pipeline.last_resource_report
        await pipeline.close()
        assert isinstance(result, BaseException)
        assert report is not None
        return result, report

    error, report = asyncio.run(scenario())

    assert isinstance(error, ExceptionGroup)
    assert report.terminal == "failed"
    assert report.reason == "pipeline_failed"
    assert report.max_temp_bytes <= 64 * 1024 * 1024
    assert not list(tmp_path.rglob("*.wav"))
