"""W08 dialogue terminal semantics after partial visible output."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from app.clients.llm import (
    LLMErrorCode,
    LLMProviderError,
    OpenAICompatibleLLMProvider,
    StreamCompletionMode,
)
from app.clients.tts import MockTTSProvider
from app.config import Settings
from app.config.logging import close_logging, configure_logging
from app.config.settings import LoggingConfig
from app.core import CancellationToken, TurnService
from app.diagnostics import DiagnosticExporter
from app.memory.runtime import MemoryRuntime, create_memory_runtime
from app.paths import AppPaths
from app.pipelines import DialoguePipeline
from app.pipelines.audio_player import AudioPlaybackResult, SilentAudioPlayer
from app.schemas import AudioResult, ChatCompletion, ChatRequest, InputMode, TurnState, UserMessage


class _PartialTruncatedLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, _request: ChatRequest, _token: CancellationToken) -> AsyncIterator[str]:
        self.calls += 1
        yield "可见字幕。"
        raise LLMProviderError(LLMErrorCode.truncated, retryable=False) from RuntimeError(
            "W08_PROVIDER_ERROR_CAUSE_SENTINEL"
        )

    async def complete(self, _request: ChatRequest, _token: CancellationToken) -> ChatCompletion:
        raise AssertionError("streaming pipeline must not call complete")

    async def close(self) -> None:
        return None


class _PlaybackThenTruncatedLLM(_PartialTruncatedLLM):
    def __init__(self, playback_started: asyncio.Event) -> None:
        super().__init__()
        self._playback_started = playback_started

    async def stream(self, _request: ChatRequest, _token: CancellationToken) -> AsyncIterator[str]:
        self.calls += 1
        yield "first audible sentence."
        yield " next"
        await asyncio.wait_for(self._playback_started.wait(), timeout=1)
        raise LLMProviderError(LLMErrorCode.truncated, retryable=False)


class _RecordingAudioPlayer:
    def __init__(self, playback_started: asyncio.Event) -> None:
        self._playback_started = playback_started
        self.play_calls = 0

    async def play(self, _result: AudioResult, _token: CancellationToken) -> AudioPlaybackResult:
        self.play_calls += 1
        self._playback_started.set()
        return AudioPlaybackResult(played=True)

    async def stop(self, *, immediate: bool = True) -> None:
        del immediate

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    "mode",
    [StreamCompletionMode.finish_reason, StreamCompletionMode.eof],
)
def test_incompatible_done_marker_fails_turn_and_never_persists_partial_assistant(
    tmp_path: Path,
    mode: StreamCompletionMode,
) -> None:
    async def scenario() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            partial = (
                '{"plan":{"emotion":"neutral","focused_variant":"default"},'
                '"segments":[{"text":"partial-assistant'
            )
            return httpx.Response(
                200,
                text=(
                    f"data: {json.dumps({'choices': [{'delta': {'content': partial}}]})}\n\n"
                    "data: [DONE]\n\n"
                ),
            )

        database_path = tmp_path / "state" / "w08-completion.sqlite3"
        memory = await create_memory_runtime(str(database_path))
        assert isinstance(memory, MemoryRuntime)
        llm = OpenAICompatibleLLMProvider(
            base_url="https://provider.invalid",
            model="synthetic-model",
            api_key="synthetic-key",
            stream_completion_mode=mode,
            transport=httpx.MockTransport(handler),
        )
        pipeline = DialoguePipeline(
            llm,
            MockTTSProvider(tmp_path / "audio", synthesis_delay_seconds=0),
            SilentAudioPlayer(),
            segment_min_chars=1,
            tts_worker_count=1,
            tts_connect_timeout_ms=80,
            tts_first_byte_timeout_ms=80,
            tts_total_timeout_ms=300,
            tts_cancellation_timeout_ms=50,
        )
        service = TurnService(
            logging.getLogger("w08-completion"), pipeline, observers=(memory.observer,)
        )
        state = await service.accept(UserMessage(text="synthetic completion request"))
        await service.wait_idle()

        snapshot = service.snapshot()
        assert snapshot["turns"][state.turn_id]["status"] == "failed"
        assert snapshot["turns"][state.turn_id]["error_code"] == "llm_protocol_error"

        await service.shutdown()
        await memory.close()
        with sqlite3.connect(database_path) as connection:
            rows = connection.execute(
                "SELECT role, content FROM conversation_messages ORDER BY created_at"
            ).fetchall()
        assert rows == [("user", "synthetic completion request")]
        assert all("partial-assistant" not in content for _role, content in rows)

    asyncio.run(scenario())


def test_partial_subtitle_failure_keeps_stable_terminal_and_never_retries(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        llm = _PartialTruncatedLLM()
        pipeline = DialoguePipeline(
            llm,
            MockTTSProvider(tmp_path / "audio", synthesis_delay_seconds=0),
            SilentAudioPlayer(),
            segment_min_chars=1,
            tts_worker_count=1,
            tts_connect_timeout_ms=80,
            tts_first_byte_timeout_ms=80,
            tts_total_timeout_ms=300,
            tts_cancellation_timeout_ms=50,
        )
        message = UserMessage(text="synthetic request")
        state = TurnState(
            turn_id="turn_w08_partial",
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=InputMode.text,
        )
        token = CancellationToken("w08-partial")
        events: list[tuple[str, dict[str, object]]] = []

        async def emit(event_type: str, payload: dict[str, object]) -> None:
            events.append((event_type, payload))

        with pytest.raises(LLMProviderError) as captured:
            await pipeline.run(message, state, token, emit)

        assert captured.value.code is LLMErrorCode.truncated
        assert llm.calls == 1
        assert any(event == "assistant.delta" for event, _payload in events)
        incomplete = [
            payload for event, payload in events if event == "assistant.output_incomplete"
        ]
        assert incomplete == [
            {
                "error_code": "llm_truncated",
                "visible_output": True,
                "playback_started": False,
            }
        ]
        serialized = repr(events)
        assert "synthetic request" not in serialized
        assert "provider body" not in serialized
        assert pipeline.last_resource_report is not None
        assert pipeline.last_resource_report.terminal == "failed"
        assert pipeline.last_resource_report.reason == "llm_truncated"
        await pipeline.close()
        assert not list(tmp_path.rglob("*.wav"))

    asyncio.run(scenario())


def test_error_after_partial_playback_never_retries_or_reports_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        playback_started = asyncio.Event()
        llm = _PlaybackThenTruncatedLLM(playback_started)
        player = _RecordingAudioPlayer(playback_started)
        pipeline = DialoguePipeline(
            llm,
            MockTTSProvider(tmp_path / "audio", synthesis_delay_seconds=0),
            player,
            segment_min_chars=1,
            tts_worker_count=1,
            tts_connect_timeout_ms=80,
            tts_first_byte_timeout_ms=80,
            tts_total_timeout_ms=300,
            tts_cancellation_timeout_ms=50,
        )
        message = UserMessage(text="synthetic request")
        state = TurnState(
            turn_id="turn_w08_partial_playback",
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=InputMode.text,
        )
        events: list[tuple[str, dict[str, object]]] = []

        async def emit(event_type: str, payload: dict[str, object]) -> None:
            events.append((event_type, payload))

        with pytest.raises(LLMProviderError, match="llm_truncated"):
            await pipeline.run(
                message,
                state,
                CancellationToken("w08-partial-playback"),
                emit,
            )

        assert llm.calls == 1
        assert player.play_calls == 1
        assert [payload for event, payload in events if event == "assistant.output_incomplete"] == [
            {
                "error_code": "llm_truncated",
                "visible_output": True,
                "playback_started": True,
            }
        ]
        assert not any(event == "assistant.completed" for event, _payload in events)
        assert pipeline.last_resource_report is not None
        assert pipeline.last_resource_report.terminal == "failed"
        assert pipeline.last_resource_report.reason == "llm_truncated"
        await pipeline.close()
        assert not list(tmp_path.rglob("*.wav"))

    asyncio.run(scenario())


def test_failure_sentinels_never_enter_log_history_health_or_diagnostics(tmp_path: Path) -> None:
    async def scenario() -> None:
        paths = AppPaths(root=tmp_path / "LocalAppData" / "MeguminCompanion")
        paths.temp.mkdir(parents=True)
        settings = Settings(
            logging=LoggingConfig(
                console_enabled=False,
                file_enabled=True,
                file_path=Path("w08.jsonl"),
            )
        )
        settings._paths = paths
        logger = configure_logging(settings)
        database_path = paths.state / "companion.sqlite3"
        memory = await create_memory_runtime(str(database_path))
        assert isinstance(memory, MemoryRuntime)

        provider_body = "W08_PROVIDER_BODY_SENTINEL"
        sentinels = (
            provider_body,
            "https://user:password@provider.example/private",
            "W08_PROVIDER_TOKEN_SENTINEL",
            "-----BEGIN CERTIFICATE-----W08_SENTINEL",
            str(tmp_path / "sensitive-provider-path"),
        )

        class _SentinelLLM(_PartialTruncatedLLM):
            async def stream(
                self, _request: ChatRequest, _token: CancellationToken
            ) -> AsyncIterator[str]:
                self.calls += 1
                yield provider_body
                raise LLMProviderError(LLMErrorCode.truncated, retryable=False) from RuntimeError(
                    "|".join(sentinels)
                )

        pipeline = DialoguePipeline(
            _SentinelLLM(),
            MockTTSProvider(paths.temp / "audio", synthesis_delay_seconds=0),
            SilentAudioPlayer(),
            segment_min_chars=1,
            tts_worker_count=1,
            tts_connect_timeout_ms=80,
            tts_first_byte_timeout_ms=80,
            tts_total_timeout_ms=300,
            tts_cancellation_timeout_ms=50,
        )
        service = TurnService(logger, pipeline, observers=(memory.observer,))
        state = await service.accept(UserMessage(text="synthetic history-safe request"))
        await service.wait_idle()
        snapshot = service.snapshot()
        assert snapshot["turns"][state.turn_id]["status"] == "failed"
        assert snapshot["turns"][state.turn_id]["error_code"] == "llm_truncated"
        health = repr(await memory.check_health())

        await service.shutdown()
        await memory.close()
        close_logging(logger)

        with sqlite3.connect(database_path) as connection:
            history = json.dumps(
                connection.execute(
                    "SELECT role, content FROM conversation_messages ORDER BY created_at"
                ).fetchall(),
                ensure_ascii=False,
            )
        logs = b"".join(path.read_bytes() for path in paths.logs.glob("*.jsonl"))
        destination = tmp_path / "exports" / "w08-diagnostics.zip"
        result = DiagnosticExporter(
            paths,
            known_secrets=sentinels,
            forbidden_values=sentinels,
        ).export(destination)
        assert result.archive_created
        with zipfile.ZipFile(destination) as archive:
            diagnostic = b"".join(archive.read(name) for name in archive.namelist())

        surfaces = (history, health, logs.decode("utf-8"), diagnostic.decode("utf-8"))
        for sentinel in sentinels:
            assert all(sentinel not in surface for surface in surfaces)
        assert "synthetic history-safe request" in history

    asyncio.run(scenario())
