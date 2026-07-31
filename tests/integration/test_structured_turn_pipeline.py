"""Streaming structured replies stay sanitized and follow ordered playback."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TypedDict

import pytest
from app.clients.llm.errors import LLMErrorCode, LLMProviderError
from app.clients.tts import MockTTSProvider
from app.core import CancellationToken
from app.core.cancellation import TurnCancelledError
from app.pipelines import DialoguePipeline
from app.pipelines.audio_player import AudioPlaybackResult
from app.schemas import (
    AudioResult,
    ChatCompletion,
    ChatRequest,
    TTSJob,
    TurnOutcome,
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
    "tts_total_timeout_ms": 500,
    "tts_cancellation_timeout_ms": 50,
}


def _structured_payload(
    *,
    emotion: str = "focused",
    variant: str = "chuunibyou",
    segments: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "plan": {
                "emotion": emotion,
                "focused_variant": variant,
            },
            "segments": segments
            or [
                {"text": "第一段中文已经准备好了。", "red_eye": False},
                {"text": "第二段中文也已经准备好了。", "red_eye": False},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class _StructuredLLM:
    turn_stream_format = "avatar_json"

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.requests: list[ChatRequest] = []
        self.closed = False

    async def stream(
        self,
        request: ChatRequest,
        token: CancellationToken,
    ) -> AsyncIterator[str]:
        self.requests.append(request)
        for chunk in self._chunks:
            token.raise_if_cancelled()
            yield chunk
            await asyncio.sleep(0)

    async def complete(
        self,
        request: ChatRequest,
        token: CancellationToken,
    ) -> ChatCompletion:
        token.raise_if_cancelled()
        self.requests.append(request)
        return ChatCompletion(text="")

    async def close(self) -> None:
        self.closed = True


class _BarrierStructuredLLM(_StructuredLLM):
    def __init__(self, first: str, second: str, barrier: asyncio.Event) -> None:
        super().__init__([])
        self._first = first
        self._second = second
        self._barrier = barrier

    async def stream(
        self,
        request: ChatRequest,
        token: CancellationToken,
    ) -> AsyncIterator[str]:
        self.requests.append(request)
        yield self._first
        await self._barrier.wait()
        token.raise_if_cancelled()
        yield self._second


class _RecordingTTS(MockTTSProvider):
    def __init__(
        self,
        root: Path,
        *,
        duration_ms: int,
        synthesis_delay_seconds: float,
        delay_by_index: dict[int, float],
    ) -> None:
        super().__init__(
            root,
            duration_ms=duration_ms,
            synthesis_delay_seconds=synthesis_delay_seconds,
            delay_by_index=delay_by_index,
        )
        self.jobs: list[TTSJob] = []

    async def synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult:
        self.jobs.append(job)
        return await super().synthesize(
            job,
            segment_index=segment_index,
            token=token,
        )


class _RecordingPlayer:
    def __init__(self) -> None:
        self.indices: list[int] = []
        self.first_played = asyncio.Event()
        self.stop_count = 0

    async def play(
        self,
        result: AudioResult,
        token: CancellationToken,
    ) -> AudioPlaybackResult:
        token.raise_if_cancelled()
        assert result.audio_path is not None
        index = int(result.audio_path.name.split("-", 1)[0])
        self.indices.append(index)
        self.first_played.set()
        return AudioPlaybackResult(played=True)

    async def stop(self, *, immediate: bool = False) -> None:
        del immediate
        self.stop_count += 1

    async def close(self) -> None:
        return None


def _state(message: UserMessage) -> TurnState:
    return TurnState(
        session_id=message.session_id,
        source_message_id=message.message_id,
        input_mode=message.input_mode,
    )


def test_structured_segments_are_sanitized_and_played_in_order(tmp_path: Path) -> None:
    async def scenario() -> tuple[
        TurnOutcome,
        list[tuple[str, dict[str, object]]],
        _StructuredLLM,
        _RecordingTTS,
        _RecordingPlayer,
    ]:
        raw = _structured_payload()
        llm = _StructuredLLM([raw[index : index + 3] for index in range(0, len(raw), 3)])
        tts = _RecordingTTS(
            tmp_path,
            duration_ms=1,
            synthesis_delay_seconds=0,
            delay_by_index={0: 0.04, 1: 0.001},
        )
        player = _RecordingPlayer()
        pipeline = DialoguePipeline(llm, tts, player, **_TTS_DEADLINES)
        message = UserMessage(text="请给出结构化中文回复")
        state = _state(message)
        events: list[tuple[str, dict[str, object]]] = []

        async def emit(name: str, payload: dict[str, object]) -> None:
            events.append((name, payload))

        outcome = await pipeline.run(
            message,
            state,
            CancellationToken(state.turn_id),
            emit,
        )
        await pipeline.close()
        return outcome, events, llm, tts, player

    outcome, events, llm, tts, player = asyncio.run(scenario())

    assert outcome.full_text == "第一段中文已经准备好了。第二段中文也已经准备好了。"
    assert player.indices == [0, 1]
    assert [job.style for job in tts.jobs] == ["focused", "focused"]
    assert [job.speed_factor for job in tts.jobs] == [0.95, 0.95]
    assert llm.requests[0].response_format == "json_object"
    assert llm.closed

    plan_payloads = [payload for name, payload in events if name == "avatar.plan"]
    assert plan_payloads == [
        {
            "emotion": "focused",
            "focused_variant": "chuunibyou",
        }
    ]
    visible = [
        (name, payload)
        for name, payload in events
        if name in {"assistant.delta", "assistant.segment"}
    ]
    assert [payload["delta"] for name, payload in visible if name == "assistant.delta"] == [
        "第一段中文已经准备好了。",
        "第二段中文也已经准备好了。",
    ]
    for name, payload in visible:
        serialized = json.dumps(payload, ensure_ascii=False)
        assert '"plan"' not in serialized
        assert "focused_variant" not in payload
        assert "red_eye" not in payload
        if name == "assistant.segment":
            assert payload["is_final"] is True

    event_names = [name for name, _payload in events]
    first_started = event_names.index("playback.started")
    first_red_eye = event_names.index("avatar.red_eye")
    assert first_red_eye == first_started + 1
    assert event_names.count("avatar.red_eye") == 1
    assert not list(tmp_path.rglob("*.wav"))


def test_invalid_tail_keeps_played_segment_and_drops_invalid_unplayed_text(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[tuple[str, dict[str, object]]], str]:
        player = _RecordingPlayer()
        first = (
            '{"plan":{"emotion":"happy","focused_variant":"default"},'
            '"segments":[{"text":"第一段已经播放。","red_eye":false},'
        )
        second = '{"text":"不得显示的内容","red_eye":false,"unknown":1}]}'
        llm = _BarrierStructuredLLM(first, second, player.first_played)
        pipeline = DialoguePipeline(
            llm,
            MockTTSProvider(tmp_path, duration_ms=1, synthesis_delay_seconds=0),
            player,
            **_TTS_DEADLINES,
        )
        message = UserMessage(text="验证无效尾部")
        state = _state(message)
        events: list[tuple[str, dict[str, object]]] = []

        async def emit(name: str, payload: dict[str, object]) -> None:
            events.append((name, payload))

        with pytest.raises(LLMProviderError) as caught:
            await pipeline.run(
                message,
                state,
                CancellationToken(state.turn_id),
                emit,
            )
        await pipeline.close()
        assert caught.value.code is LLMErrorCode.structured
        return events, caught.value.code.value

    events, code = asyncio.run(scenario())

    deltas = [payload["delta"] for name, payload in events if name == "assistant.delta"]
    assert deltas == ["第一段已经播放。"]
    assert "不得显示的内容" not in json.dumps(events, ensure_ascii=False)
    assert [
        payload["error_code"] for name, payload in events if name == "assistant.output_incomplete"
    ] == [code]
    assert not list(tmp_path.rglob("*.wav"))


def test_cancellation_prevents_late_audio_and_red_eye(tmp_path: Path) -> None:
    async def scenario() -> tuple[
        list[tuple[str, dict[str, object]]],
        int,
    ]:
        raw = _structured_payload(
            emotion="happy",
            variant="default",
            segments=[
                {"text": "第一段播放。", "red_eye": True},
                {"text": "第二段不得迟到。", "red_eye": True},
            ],
        )
        llm = _StructuredLLM([raw])
        player = _RecordingPlayer()
        pipeline = DialoguePipeline(
            llm,
            MockTTSProvider(
                tmp_path,
                duration_ms=1,
                synthesis_delay_seconds=0,
                delay_by_index={0: 0.001, 1: 0.2},
            ),
            player,
            tts_worker_count=2,
            **_TTS_DEADLINES,
        )
        message = UserMessage(text="验证取消屏障")
        state = _state(message)
        token = CancellationToken(state.turn_id)
        events: list[tuple[str, dict[str, object]]] = []

        async def emit(name: str, payload: dict[str, object]) -> None:
            events.append((name, payload))

        run_task = asyncio.create_task(pipeline.run(message, state, token, emit))
        await asyncio.wait_for(player.first_played.wait(), timeout=2)
        token.cancel()
        with pytest.raises(TurnCancelledError):
            await run_task
        red_eye_count = sum(name == "avatar.red_eye" for name, _payload in events)
        await asyncio.sleep(0.25)
        assert sum(name == "avatar.red_eye" for name, _payload in events) == red_eye_count
        await pipeline.close()
        return events, red_eye_count

    events, red_eye_count = asyncio.run(scenario())

    assert red_eye_count == 1
    assert [payload["delta"] for name, payload in events if name == "assistant.delta"] == [
        "第一段播放。"
    ]
    assert not any(name == "playback.started" and payload["index"] == 1 for name, payload in events)
    assert not list(tmp_path.rglob("*.wav"))
