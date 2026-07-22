"""Deterministic mock LLM and legal WAV generation tests."""

import asyncio
import threading
import wave
from pathlib import Path

import pytest
from app.clients.llm import MockLLMProvider
from app.clients.tts import MockTTSProvider
from app.clients.tts import mock_tts as mock_tts_module
from app.core import CancellationToken
from app.paths import AppPaths
from app.schemas import TTSJob, UserMessage
from app.temp_assets import TempAssetRegistry
from app.windows_security import PortableDirectorySecurity


@pytest.mark.parametrize(
    "options",
    [
        {"sample_rate": 0},
        {"duration_ms": -1},
        {"volume": -0.01},
        {"volume": 1.01},
        {"synthesis_delay_seconds": -0.01},
    ],
)
def test_mock_tts_rejects_invalid_synthesis_parameters(
    tmp_path: Path,
    options: dict[str, int | float],
) -> None:
    with pytest.raises(ValueError):
        MockTTSProvider(tmp_path, **options)  # type: ignore[arg-type]


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


def test_mock_tts_total_deadline_covers_wave_generation_and_registry_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    original_write = provider._write_wave
    write_started = threading.Event()
    release_write = threading.Event()

    def controlled_slow_write(path: Path, frequency_hz: float) -> None:
        write_started.set()
        assert release_write.wait(timeout=1)
        original_write(path, frequency_hz)

    monkeypatch.setattr(provider, "_write_wave", controlled_slow_write)

    async def scenario() -> None:
        token = CancellationToken("turn_mock_deadline")
        controller_error: list[BaseException] = []

        def release_after_deadline() -> None:
            try:
                assert write_started.wait(timeout=1)
                assert not release_write.wait(timeout=0.15)
                release_write.set()
            except BaseException as exc:
                controller_error.append(exc)
                release_write.set()

        controller = threading.Thread(target=release_after_deadline)
        controller.start()
        task = asyncio.create_task(
            provider.synthesize(
                TTSJob(
                    turn_id="turn_mock_deadline",
                    segment_id="segment_mock_deadline",
                    text="synthetic",
                    connect_timeout_ms=5,
                    first_byte_timeout_ms=5,
                    timeout_ms=100,
                    cancellation_timeout_ms=200,
                    cancellation_token_id=token.token_id,
                ),
                segment_index=0,
                token=token,
            )
        )
        try:
            result = await asyncio.wait_for(task, timeout=1)
        finally:
            controller.join(timeout=1)

        assert controller_error == []
        assert write_started.is_set()
        assert not result.success
        assert result.error_code == "tts_total_timeout"
        assert registry.entries() == ()
        assert not list(paths.temp.rglob("*.wav"))
        await provider.close()

    asyncio.run(scenario())


def test_mock_tts_total_deadline_also_covers_configured_delay(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = MockTTSProvider(
            tmp_path,
            duration_ms=0,
            synthesis_delay_seconds=1,
        )
        token = CancellationToken("turn_mock_delay_timeout")
        result = await provider.synthesize(
            TTSJob(
                turn_id="turn_mock_delay_timeout",
                segment_id="segment_mock_delay_timeout",
                text="synthetic",
                connect_timeout_ms=5,
                first_byte_timeout_ms=5,
                timeout_ms=5,
                cancellation_timeout_ms=50,
                cancellation_token_id=token.token_id,
            ),
            segment_index=0,
            token=token,
        )

        assert not result.success
        assert result.error_code == "tts_total_timeout"
        await provider.discard(result)
        await provider.close()

    asyncio.run(scenario())


def test_mock_tts_cancelled_during_configured_delay_never_creates_a_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = MockTTSProvider(
            tmp_path,
            duration_ms=0,
            synthesis_delay_seconds=1,
        )
        token = CancellationToken("turn_mock_delay_cancel")
        synthesis = asyncio.create_task(
            provider.synthesize(
                TTSJob(
                    turn_id="turn_mock_delay_cancel",
                    segment_id="segment_mock_delay_cancel",
                    text="synthetic",
                    connect_timeout_ms=80,
                    first_byte_timeout_ms=80,
                    timeout_ms=300,
                    cancellation_timeout_ms=50,
                    cancellation_token_id=token.token_id,
                ),
                segment_index=0,
                token=token,
            )
        )
        await asyncio.sleep(0)
        token.cancel()

        with pytest.raises(asyncio.CancelledError):
            await synthesis
        assert not list(tmp_path.rglob("*.wav"))
        await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_wave", [b"not-a-wave", b""])
def test_mock_tts_invalid_generated_wave_is_rejected_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_wave: bytes,
) -> None:
    async def scenario() -> None:
        provider = MockTTSProvider(
            tmp_path,
            duration_ms=0,
            synthesis_delay_seconds=0,
        )

        def write_invalid(path: Path, _frequency_hz: float) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(invalid_wave)

        monkeypatch.setattr(provider, "_write_wave", write_invalid)
        token = CancellationToken("turn_mock_invalid_wave")
        with pytest.raises(ValueError, match="mock wav validation failed"):
            await provider.synthesize(
                TTSJob(
                    turn_id="turn_mock_invalid_wave",
                    segment_id="segment_mock_invalid_wave",
                    text="synthetic",
                    connect_timeout_ms=80,
                    first_byte_timeout_ms=80,
                    # The validation path uses owned worker threads.  Leave
                    # scheduler headroom; this test asserts invalid WAV
                    # rejection, not a sub-second total deadline.
                    timeout_ms=3_000,
                    cancellation_timeout_ms=50,
                    cancellation_token_id=token.token_id,
                ),
                segment_index=0,
                token=token,
            )

        assert not list(tmp_path.rglob("*.wav"))
        await provider.close()

    asyncio.run(scenario())


def test_mock_tts_generated_wave_metadata_is_validated_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = MockTTSProvider(
            tmp_path,
            duration_ms=0,
            synthesis_delay_seconds=0,
        )

        def write_stereo(path: Path, _frequency_hz: float) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48_000)
                output.writeframes(b"")

        monkeypatch.setattr(provider, "_write_wave", write_stereo)
        token = CancellationToken("turn_mock_invalid_metadata")
        with pytest.raises(ValueError, match="mock wav validation failed"):
            await provider.synthesize(
                TTSJob(
                    turn_id="turn_mock_invalid_metadata",
                    segment_id="segment_mock_invalid_metadata",
                    text="synthetic",
                    connect_timeout_ms=80,
                    first_byte_timeout_ms=80,
                    timeout_ms=300,
                    cancellation_timeout_ms=50,
                    cancellation_token_id=token.token_id,
                ),
                segment_index=0,
                token=token,
            )

        assert not list(tmp_path.rglob("*.wav"))
        await provider.close()

    asyncio.run(scenario())


def test_mock_tts_owned_file_operation_rejects_an_expired_deadline() -> None:
    async def scenario() -> None:
        called = False

        def operation() -> None:
            nonlocal called
            called = True

        loop = asyncio.get_running_loop()
        with pytest.raises(TimeoutError):
            await mock_tts_module._run_owned_thread(
                operation,
                token=CancellationToken("mock-expired-owned-operation"),
                deadline=loop.time(),
            )
        with pytest.raises(TimeoutError):
            MockTTSProvider._raise_if_expired(loop.time())
        assert not called

    asyncio.run(scenario())


def test_mock_tts_cancellation_during_wave_generation_cleans_registry_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        paths = AppPaths(root=tmp_path / "private-cancel")
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
        token = CancellationToken("turn_mock_cancel")
        loop = asyncio.get_running_loop()
        original_write = provider._write_wave

        def cancel_after_write(path: Path, frequency_hz: float) -> None:
            original_write(path, frequency_hz)
            loop.call_soon_threadsafe(token.cancel)

        monkeypatch.setattr(provider, "_write_wave", cancel_after_write)
        with pytest.raises(asyncio.CancelledError):
            await provider.synthesize(
                TTSJob(
                    turn_id="turn_mock_cancel",
                    segment_id="segment_mock_cancel",
                    text="synthetic",
                    connect_timeout_ms=80,
                    first_byte_timeout_ms=80,
                    timeout_ms=300,
                    cancellation_timeout_ms=50,
                    cancellation_token_id=token.token_id,
                ),
                segment_index=0,
                token=token,
            )

        assert registry.entries() == ()
        assert not list(paths.temp.rglob("*.wav"))
        await provider.close()

    asyncio.run(scenario())


def test_mock_tts_repeated_waiter_cancellation_drains_owned_writer_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        paths = AppPaths(root=tmp_path / "private-repeated-cancel")
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
        original_write = provider._write_wave
        write_started = threading.Event()
        release_write = threading.Event()

        def controlled_write(path: Path, frequency_hz: float) -> None:
            write_started.set()
            assert release_write.wait(timeout=1)
            original_write(path, frequency_hz)

        monkeypatch.setattr(provider, "_write_wave", controlled_write)
        token = CancellationToken("turn_mock_repeated_cancel")
        synthesis = asyncio.create_task(
            provider.synthesize(
                TTSJob(
                    turn_id="turn_mock_repeated_cancel",
                    segment_id="segment_mock_repeated_cancel",
                    text="synthetic",
                    connect_timeout_ms=80,
                    first_byte_timeout_ms=80,
                    timeout_ms=500,
                    cancellation_timeout_ms=100,
                    cancellation_token_id=token.token_id,
                ),
                segment_index=0,
                token=token,
            )
        )
        assert await asyncio.to_thread(write_started.wait, 1)
        synthesis.cancel()
        synthesis.cancel()
        await asyncio.sleep(0)
        assert not synthesis.done()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(synthesis, timeout=1)

        assert registry.entries() == ()
        assert not list(paths.temp.rglob("*.wav"))
        await provider.close()

    asyncio.run(scenario())
