"""Deterministic mock LLM and legal WAV generation tests."""

import asyncio
import wave
from pathlib import Path

import pytest
from app.clients.llm import MockLLMProvider
from app.clients.tts import MockTTSProvider
from app.core import CancellationToken
from app.paths import AppPaths
from app.pipelines.audio_player import SystemAudioPlayer
from app.schemas import TTSJob, UserMessage
from app.temp_assets import TempAssetRegistry
from app.windows_security import PortableDirectorySecurity


def test_mock_llm_emits_exact_controllable_deltas() -> None:
    async def scenario() -> list[str]:
        provider = MockLLMProvider(deltas=["第一", "句。", "第二句！"], token_delay_seconds=0)
        token = CancellationToken("turn_test")
        return [delta async for delta in provider.stream(UserMessage(text="测试"), token)]

    assert asyncio.run(scenario()) == ["第一", "句。", "第二句！"]


def test_mock_llm_stops_before_later_tokens_when_cancelled() -> None:
    async def scenario() -> list[str]:
        provider = MockLLMProvider(deltas=["一", "二", "三"], token_delay_seconds=0.05)
        token = CancellationToken("turn_test")
        output = []
        async for delta in provider.stream(UserMessage(text="测试"), token):
            output.append(delta)
            token.cancel()
        return output

    assert asyncio.run(scenario()) == ["一"]


def test_mock_tts_writes_valid_pcm_wave_and_discards_it(tmp_path: Path) -> None:
    async def scenario() -> tuple[Path, int, int, int]:
        provider = MockTTSProvider(
            tmp_path,
            sample_rate=16_000,
            duration_ms=100,
            volume=0.1,
            synthesis_delay_seconds=0,
        )
        token = CancellationToken("turn_test")
        result = await provider.synthesize(
            TTSJob(
                turn_id="turn_test",
                segment_id="segment_test",
                text="纯合成测试提示音",
                connect_timeout_ms=80,
                first_byte_timeout_ms=80,
                timeout_ms=300,
                cancellation_timeout_ms=50,
                cancellation_token_id=token.token_id,
            ),
            segment_index=2,
            token=token,
        )
        assert result.audio_path is not None
        path = result.audio_path
        with wave.open(str(path), "rb") as audio:
            shape = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate())
        await provider.discard(result)
        return path, *shape

    path, channels, sample_width, sample_rate = asyncio.run(scenario())

    assert (channels, sample_width, sample_rate) == (1, 2, 16_000)
    assert not path.exists()


def test_mock_tts_registers_before_write_and_unregisters_after_discard(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        paths = AppPaths(root=tmp_path / "private")
        registry = TempAssetRegistry(
            paths,
            minimum_scavenge_age_seconds=0.0,
            directory_security=PortableDirectorySecurity(),
        )
        provider = MockTTSProvider(
            paths.temp / "audio" / "mock",
            duration_ms=0,
            synthesis_delay_seconds=0,
            temp_registry=registry,
        )
        token = CancellationToken("turn_registry")
        result = await provider.synthesize(
            TTSJob(
                turn_id="turn_registry",
                segment_id="segment_registry",
                text="registry",
                connect_timeout_ms=80,
                first_byte_timeout_ms=80,
                timeout_ms=300,
                cancellation_timeout_ms=50,
                cancellation_token_id=token.token_id,
            ),
            segment_index=0,
            token=token,
        )

        assert result.audio_path is not None and result.audio_path.exists()
        assert [entry.relative_path for entry in registry.entries()] == [
            result.audio_path.relative_to(paths.temp).as_posix()
        ]
        await provider.discard(result)
        assert registry.entries() == ()
        assert not result.audio_path.exists()

    asyncio.run(scenario())


def test_system_player_reuses_one_stream_for_adjacent_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRawOutputStream:
        def __init__(self, **_options: object) -> None:
            self.active = False
            self.start_count = 0
            self.write_count = 0
            self.stop_count = 0
            self.abort_count = 0
            self.close_count = 0

        def start(self) -> None:
            self.active = True
            self.start_count += 1

        def write(self, _frames: bytes) -> None:
            self.write_count += 1

        def abort(self) -> None:
            self.active = False
            self.abort_count += 1

        def stop(self) -> None:
            self.active = False
            self.stop_count += 1

        def close(self) -> None:
            self.close_count += 1

    streams: list[FakeRawOutputStream] = []

    def make_stream(**options: object) -> FakeRawOutputStream:
        stream = FakeRawOutputStream(**options)
        streams.append(stream)
        return stream

    monkeypatch.setattr("app.pipelines.audio_player.sd.RawOutputStream", make_stream)

    async def scenario() -> None:
        provider = MockTTSProvider(
            tmp_path,
            sample_rate=48_000,
            duration_ms=10,
            synthesis_delay_seconds=0,
        )
        token = CancellationToken("turn_test")
        results = []
        for index in range(2):
            result = await provider.synthesize(
                TTSJob(
                    turn_id="turn_test",
                    segment_id=f"segment_{index}",
                    text=f"第 {index} 段",
                    connect_timeout_ms=80,
                    first_byte_timeout_ms=80,
                    timeout_ms=300,
                    cancellation_timeout_ms=50,
                    cancellation_token_id=token.token_id,
                ),
                segment_index=index,
                token=token,
            )
            results.append(result)

        player = SystemAudioPlayer()
        for result in results:
            await player.play(result, token)
        await player.close()

        interrupted_player = SystemAudioPlayer()
        await interrupted_player.play(results[0], token)
        await interrupted_player.stop(immediate=True)
        for result in results:
            await provider.discard(result)

    asyncio.run(scenario())

    assert len(streams) == 2
    assert streams[0].start_count == 1
    assert streams[0].write_count == 2
    assert streams[0].stop_count == 1
    assert streams[0].abort_count == 0
    assert streams[0].close_count == 1
    assert streams[1].stop_count == 0
    assert streams[1].abort_count == 1
    assert streams[1].close_count == 1
