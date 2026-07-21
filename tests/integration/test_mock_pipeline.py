"""Ordering, cancellation barrier, cleanup, and metrics for the Gate A pipeline."""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TypedDict

from app.clients.llm import MockLLMProvider
from app.clients.tts import MockTTSProvider
from app.core import CancellationToken, TurnService
from app.pipelines import DialoguePipeline
from app.schemas import (
    AudioResult,
    ChatCompletion,
    ChatRequest,
    InputMode,
    PipelineEvent,
    ProactiveIntent,
    TurnState,
    UserMessage,
)


class _TTSDeadlines(TypedDict):
    tts_connect_timeout_ms: int
    tts_first_byte_timeout_ms: int
    tts_total_timeout_ms: int
    tts_cancellation_timeout_ms: int


_TTS_DEADLINES: _TTSDeadlines = {
    "tts_connect_timeout_ms": 80,
    "tts_first_byte_timeout_ms": 80,
    "tts_total_timeout_ms": 300,
    "tts_cancellation_timeout_ms": 50,
}


class RecordingAudioPlayer:
    def __init__(self) -> None:
        self.played_indices: list[int] = []
        self.stop_count = 0
        self.close_calls = 0

    async def play(self, result: AudioResult, token: CancellationToken) -> None:
        token.raise_if_cancelled()
        assert result.audio_path is not None
        self.played_indices.append(int(result.audio_path.name.split("-", 1)[0]))

    async def stop(self, *, immediate: bool = False) -> None:
        self.stop_count += 1

    async def close(self) -> None:
        self.close_calls += 1


class CapturingLLM:
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.close_calls = 0

    async def stream(self, request: ChatRequest, token: CancellationToken) -> AsyncIterator[str]:
        token.raise_if_cancelled()
        self.requests.append(request)
        yield "主动问候完成。"

    async def complete(self, request: ChatRequest, token: CancellationToken) -> ChatCompletion:
        token.raise_if_cancelled()
        self.requests.append(request)
        return ChatCompletion(text="主动问候完成。")

    async def close(self) -> None:
        self.close_calls += 1


class FirstTurnBarrierLLM(CapturingLLM):
    """Keep the first stream active until the test submits a replacement turn."""

    def __init__(self) -> None:
        super().__init__()
        self.first_turn_blocked = asyncio.Event()
        self._stream_count = 0

    async def stream(self, request: ChatRequest, token: CancellationToken) -> AsyncIterator[str]:
        token.raise_if_cancelled()
        self.requests.append(request)
        self._stream_count += 1
        if self._stream_count == 1:
            yield "old turn remains active."
            self.first_turn_blocked.set()
            await asyncio.Event().wait()
            return
        yield "replacement turn completed."


def test_out_of_order_tts_is_played_in_segment_order_and_cleaned(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[int], list[int], dict[str, object]]:
        player = RecordingAudioPlayer()
        pipeline = DialoguePipeline(
            MockLLMProvider(
                deltas=["第一句话已经准备好了。", "第二句话也准备好了。", "第三句话完成！"],
                token_delay_seconds=0,
            ),
            MockTTSProvider(
                tmp_path,
                duration_ms=1,
                synthesis_delay_seconds=0,
                delay_by_index={0: 0.04, 1: 0.001, 2: 0.001},
            ),
            player,
            tts_worker_count=2,
            **_TTS_DEADLINES,
        )
        message = UserMessage(text="开始测试")
        state = TurnState(
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
        )
        token = CancellationToken(state.turn_id)
        events: list[tuple[str, dict[str, object]]] = []

        async def emit(event_type: str, payload: dict[str, object]) -> None:
            events.append((event_type, payload))

        outcome = await pipeline.run(message, state, token, emit)
        ready = []
        for name, payload in events:
            if name == "audio.ready":
                index = payload["index"]
                assert isinstance(index, int)
                ready.append(index)
        await pipeline.close()
        return ready, player.played_indices, outcome.metrics.model_dump(mode="json")

    ready, played, metrics = asyncio.run(scenario())

    assert ready != sorted(ready)
    assert played == [0, 1, 2]
    assert metrics["segment_count"] == 3
    assert metrics["playback_count"] == 3
    assert metrics["llm_first_token_ms"] is not None
    assert metrics["first_sentence_play_ms"] is not None
    assert not list(tmp_path.rglob("*.wav"))


def test_new_input_is_a_hard_barrier_for_old_turn_events(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str, list[tuple[str, str]]]:
        llm = FirstTurnBarrierLLM()
        pipeline = DialoguePipeline(
            llm,
            MockTTSProvider(tmp_path, duration_ms=100, synthesis_delay_seconds=0.01),
            RecordingAudioPlayer(),
            **_TTS_DEADLINES,
        )
        logger = logging.getLogger("test.turn_barrier")
        logger.addHandler(logging.NullHandler())
        service = TurnService(logger, pipeline)
        queue = await service.subscribe("local_session")
        try:
            first = await service.accept(UserMessage(text="第一次", input_mode=InputMode.text))
            await asyncio.wait_for(llm.first_turn_blocked.wait(), timeout=2)
            second = await service.accept(UserMessage(text="第二次", input_mode=InputMode.voice))

            observed: list[tuple[str, str]] = []
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=2)
                assert isinstance(event, PipelineEvent)
                assert event.turn_id is not None
                observed.append((event.type, event.turn_id))
                queue.task_done()
                if event.type == "assistant.completed" and event.turn_id == second.turn_id:
                    break
            return first.turn_id, second.turn_id, observed
        finally:
            await service.shutdown()

    first_id, second_id, observed = asyncio.run(scenario())
    new_accepted_index = observed.index(("turn.accepted", second_id))

    assert ("turn.cancelled", first_id) in observed[:new_accepted_index]
    assert all(turn_id != first_id for _event_type, turn_id in observed[new_accepted_index:])
    assert ("assistant.completed", second_id) in observed


def test_text_only_proactive_turn_has_no_user_message_or_audio_work(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], ChatRequest, int, int]:
        llm = CapturingLLM()
        tts = MockTTSProvider(tmp_path, duration_ms=1, synthesis_delay_seconds=0)
        player = RecordingAudioPlayer()
        pipeline = DialoguePipeline(llm, tts, player, **_TTS_DEADLINES)
        intent = ProactiveIntent(
            trigger_type="idle",
            instruction="进行一次低打扰问候",
            score=0.8,
            reason="test",
            voice_allowed=False,
        )
        state = TurnState(
            session_id="local_session",
            source_message_id=intent.intent_id,
            input_mode=InputMode.text,
        )
        token = CancellationToken(state.turn_id)
        events: list[str] = []

        async def emit(event_type: str, _payload: dict[str, object]) -> None:
            events.append(event_type)

        outcome = await pipeline.run_proactive(intent, state, token, emit)
        assert outcome.full_text == "主动问候完成。"
        await pipeline.close()
        return events, llm.requests[0], player.stop_count, llm.close_calls

    events, request, stop_count, close_calls = asyncio.run(scenario())

    assert events == ["assistant.delta", "assistant.segment"]
    assert "PROACTIVE_INTENT_DATA" in str(request.messages[-1].content)
    assert "score" not in str(request.messages[-1].content)
    assert stop_count == 0
    assert close_calls == 1
    assert not list(tmp_path.rglob("*.wav"))


def test_pipeline_close_attempts_all_resources_once_after_failures(tmp_path: Path) -> None:
    async def scenario() -> tuple[tuple[object, ...], int, int, int]:
        class FailingLLM(CapturingLLM):
            async def close(self) -> None:
                await super().close()
                raise RuntimeError("private llm close failure")

        class FailingTTS(MockTTSProvider):
            def __init__(self) -> None:
                super().__init__(tmp_path, duration_ms=1)
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1
                raise RuntimeError("private tts close failure")

        class FailingPlayer(RecordingAudioPlayer):
            async def close(self) -> None:
                await super().close()
                raise RuntimeError("private player close failure")

        llm = FailingLLM()
        tts = FailingTTS()
        player = FailingPlayer()
        pipeline = DialoguePipeline(llm, tts, player, **_TTS_DEADLINES)
        results = await asyncio.gather(
            pipeline.close(),
            pipeline.close(),
            return_exceptions=True,
        )
        return results, llm.close_calls, tts.close_calls, player.close_calls

    results, llm_calls, tts_calls, player_calls = asyncio.run(scenario())

    assert all(isinstance(result, ExceptionGroup) for result in results)
    assert (llm_calls, tts_calls, player_calls) == (1, 1, 1)
