"""Offline protocol and filesystem tests for the GPT-SoVITS adapter."""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from app.clients.tts.gpt_sovits import GPTSoVITSPreset, GPTSoVITSProvider, _await_with_token
from app.core import CancellationToken
from app.paths import AppPaths
from app.schemas import AudioResult, TTSJob
from app.temp_assets import TempAssetRegistry
from app.windows_security import PortableDirectorySecurity


def _wave_bytes(*, sample_rate: int = 16_000, frame_count: int = 160) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\0\0" * frame_count)
    return output.getvalue()


def _job(token: CancellationToken, **changes: object) -> TTSJob:
    values: dict[str, object] = {
        "job_id": "job/private/id",
        "turn_id": "turn/../../escape",
        "segment_id": "segment_1",
        "text": "测试语音",
        "style": "bright",
        "speed_factor": 1.1,
        "connect_timeout_ms": 80,
        "first_byte_timeout_ms": 80,
        "timeout_ms": 300,
        "cancellation_timeout_ms": 50,
        "cancellation_token_id": token.token_id,
    }
    values.update(changes)
    return TTSJob.model_validate(values)


def _presets() -> dict[str, GPTSoVITSPreset]:
    return {
        "default": GPTSoVITSPreset(ref_audio_path="/voices/default.wav"),
        "bright": GPTSoVITSPreset(
            ref_audio_path="/voices/bright.wav",
            prompt_text="高兴",
            speed_factor=1.05,
        ),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"ref_audio_path": " "},
        {"top_k": 0},
        {"top_p": 0.0},
        {"temperature": 0.0},
        {"speed_factor": 0.0},
        {"fragment_interval": -0.1},
    ],
)
def test_preset_rejects_invalid_voice_parameters(changes: dict[str, object]) -> None:
    values: dict[str, object] = {"ref_audio_path": "/voice.wav"}
    values.update(changes)
    with pytest.raises(ValueError):
        GPTSoVITSPreset(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_seconds": 0.0},
        {"max_audio_bytes": 43},
        {"default_preset": "missing"},
        {"cache_enabled": True, "cache_dir": None},
        {"cache_max_bytes": 0},
        {"cache_ttl_seconds": 0.0},
        {"max_owned_synthesis_tasks": 0},
        {"max_owned_synthesis_tasks": 9},
    ],
)
def test_provider_rejects_unsafe_limits(tmp_path: Path, changes: dict[str, object]) -> None:
    options: dict[str, object] = {
        "base_url": "http://127.0.0.1:9880",
        "output_directory": tmp_path,
        "presets": _presets(),
    }
    options.update(changes)
    with pytest.raises(ValueError):
        GPTSoVITSProvider(**options)  # type: ignore[arg-type]


def test_reference_scope_distinguishes_local_file_from_service_resource(tmp_path: Path) -> None:
    async def scenario() -> None:
        local_reference = tmp_path / "private-user-reference.wav"
        local_reference.write_bytes(b"fixture")
        local = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path / "out-local",
            {
                "default": GPTSoVITSPreset(
                    ref_audio_path=str(local_reference),
                    ref_audio_scope="local_file",
                )
            },
        )
        await local.close()

        remote_resource = GPTSoVITSProvider(
            "https://tts.example.invalid",
            tmp_path / "out-remote",
            {
                "default": GPTSoVITSPreset(
                    ref_audio_path="/container/voice/reference.wav",
                    ref_audio_scope="service_resource",
                )
            },
        )
        await remote_resource.close()

        private_path = str(tmp_path / "missing-private-reference.wav")
        with pytest.raises(ValueError) as missing:
            GPTSoVITSProvider(
                "http://127.0.0.1:9880",
                tmp_path / "out-missing",
                {
                    "default": GPTSoVITSPreset(
                        ref_audio_path=private_path,
                        ref_audio_scope="local_file",
                    )
                },
            )
        assert private_path not in str(missing.value)

        with pytest.raises(ValueError) as remote_local:
            GPTSoVITSProvider(
                "https://tts.example.invalid",
                tmp_path / "out-invalid",
                {
                    "default": GPTSoVITSPreset(
                        ref_audio_path=str(local_reference),
                        ref_audio_scope="local_file",
                    )
                },
            )
        assert str(local_reference) not in str(remote_local.value)

        with pytest.raises(ValueError, match="unavailable"):
            GPTSoVITSProvider(
                "http://127.0.0.1:9880",
                tmp_path / "out-relative",
                {
                    "default": GPTSoVITSPreset(
                        ref_audio_path="relative-reference.wav",
                        ref_audio_scope="local_file",
                    )
                },
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "available", "error_code"),
    [(422, True, None), (404, False, "tts_protocol_error"), (503, False, "tts_unavailable")],
)
def test_probe_classifies_api_v2_without_synthesizing(
    tmp_path: Path, status: int, available: bool, error_code: str | None
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL("http://127.0.0.1:9880/tts")
            assert json.loads(request.content) == {}
            return httpx.Response(status, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider("http://127.0.0.1:9880", tmp_path, _presets(), client=client)
        result = await provider.probe(timeout_ms=50)

        assert result.available is available
        assert result.protocol == ("api_v2" if available else None)
        assert result.error_code == error_code
        await provider.close()
        assert not client.is_closed
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (httpx.ReadTimeout("slow"), "tts_timeout"),
        (httpx.ConnectError("offline"), "tts_unavailable"),
    ],
)
def test_probe_transport_failures_are_safe(
    tmp_path: Path,
    failure: httpx.RequestError,
    error_code: str,
) -> None:
    async def scenario() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            raise failure

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider("http://127.0.0.1:9880", tmp_path, _presets(), client=client)
        assert (await provider.probe(timeout_ms=50)).error_code == error_code
        await provider.close()
        assert (await provider.probe(timeout_ms=50)).error_code == "tts_closed"
        await client.aclose()

    asyncio.run(scenario())


def test_probe_requires_and_enforces_an_explicit_deadline(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
        provider = GPTSoVITSProvider("http://127.0.0.1:9880", tmp_path, _presets(), client=client)
        result = await provider.probe(timeout_ms=20)

        assert started.is_set()
        assert result.error_code == "tts_timeout"
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


def test_synthesize_applies_preset_and_atomically_lands_valid_wave(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=_wave_bytes(),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider("http://127.0.0.1:9880", tmp_path, _presets(), client=client)
        token = CancellationToken("turn_test")
        result = await provider.synthesize(_job(token), segment_index=3, token=token)

        assert result.success
        assert result.sample_rate == 16_000
        assert result.duration_ms == 10
        assert result.audio_path is not None and result.audio_path.is_file()
        private_path = str(result.audio_path.relative_to(tmp_path))
        assert "private" not in private_path
        assert "escape" not in private_path
        assert list(tmp_path.rglob("*.part")) == []
        assert requests == [
            {
                "ref_audio_path": "/voices/bright.wav",
                "prompt_text": "高兴",
                "prompt_lang": "zh",
                "text_lang": "zh",
                "top_k": 5,
                "top_p": 1.0,
                "temperature": 1.0,
                "text_split_method": "cut5",
                "batch_size": 1,
                "batch_threshold": 0.75,
                "split_bucket": True,
                "speed_factor": pytest.approx(1.155),
                "fragment_interval": 0.3,
                "seed": -1,
                "parallel_infer": True,
                "repetition_penalty": 1.35,
                "text": "测试语音",
                "media_type": "wav",
                "streaming_mode": False,
            }
        ]

        path = result.audio_path
        await provider.discard(result)
        assert not path.exists()
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (httpx.Response(401), "tts_auth_failed"),
        (httpx.Response(404), "tts_protocol_error"),
        (httpx.Response(408), "tts_timeout"),
        (httpx.Response(418), "tts_request_rejected"),
        (httpx.Response(429), "tts_rate_limited"),
        (httpx.Response(503), "tts_unavailable"),
        (
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}"),
            "tts_invalid_content_type",
        ),
        (
            httpx.Response(200, headers={"content-type": "audio/wav"}, content=b"not-wave"),
            "tts_invalid_audio",
        ),
    ],
)
def test_synthesize_maps_expected_failures_and_leaves_no_files(
    tmp_path: Path, response: httpx.Response, error_code: str
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=response.content,
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider("http://127.0.0.1:9880", tmp_path, _presets(), client=client)
        token = CancellationToken("turn_test")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)

        assert not result.success
        assert result.error_code == error_code
        assert list(tmp_path.rglob("*")) == []
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("headers", "content", "error_code"),
    [
        (
            {"content-type": "audio/wav", "content-length": "999999"},
            b"short",
            "tts_response_too_large",
        ),
        ({"content-type": "audio/wav"}, b"", "tts_invalid_audio"),
    ],
)
def test_declared_size_and_empty_body_are_rejected(
    tmp_path: Path,
    headers: dict[str, str],
    content: bytes,
    error_code: str,
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers=headers, content=content, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _presets(),
            max_audio_bytes=128,
            client=client,
        )
        token = CancellationToken("turn")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)
        assert result.error_code == error_code
        assert list(tmp_path.rglob("*")) == []
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (httpx.ReadTimeout("slow"), "tts_total_timeout"),
        (httpx.ConnectError("offline"), "tts_connection_error"),
    ],
)
def test_synthesis_transport_failures_and_closed_state(
    tmp_path: Path,
    failure: httpx.RequestError,
    error_code: str,
) -> None:
    async def scenario() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            raise failure

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider("http://127.0.0.1:9880", tmp_path, _presets(), client=client)
        token = CancellationToken("turn")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)
        assert result.error_code == error_code
        await provider.close()
        assert (
            await provider.synthesize(_job(token), segment_index=1, token=token)
        ).error_code == ("tts_closed")
        await client.aclose()

    asyncio.run(scenario())


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def test_streaming_size_limit_removes_partial_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                stream=_ChunkedStream([b"R" * 40, b"I" * 40]),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _presets(),
            max_audio_bytes=64,
            client=client,
        )
        token = CancellationToken("turn_test")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)

        assert result.error_code == "tts_response_too_large"
        assert list(tmp_path.rglob("*")) == []
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield _wave_bytes()[:20]
        self._started.set()
        await asyncio.Event().wait()
        yield b"unreachable"


def test_await_with_token_cleans_operation_after_direct_task_cancellation() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def operation() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        wrapper = asyncio.create_task(
            _await_with_token(operation(), CancellationToken("turn_direct_cancel"))
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        wrapper.cancel()

        with pytest.raises(asyncio.CancelledError):
            await wrapper

        assert cleaned.is_set()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_await_with_token_cleans_operation_after_wait_for_timeout() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def operation() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        wrapper = asyncio.create_task(
            _await_with_token(operation(), CancellationToken("turn_wait_for_timeout"))
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(wrapper, timeout=0.01)

        assert cleaned.is_set()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_cancellation_interrupts_blocked_stream_and_cleans_partial(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                stream=_BlockingStream(started),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider("http://127.0.0.1:9880", tmp_path, _presets(), client=client)
        token = CancellationToken("turn_test")
        task = asyncio.create_task(provider.synthesize(_job(token), segment_index=0, token=token))
        await asyncio.wait_for(started.wait(), timeout=1)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert list(tmp_path.rglob("*")) == []
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


def test_close_cancels_and_joins_inflight_synthesis_before_returning(tmp_path: Path) -> None:
    async def scenario() -> None:
        request_started = asyncio.Event()
        request_cleanup_started = asyncio.Event()
        finish_request_cleanup = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Model a transport that needs asynchronous cleanup and even
                # suppresses its own cancellation before producing a response.
                request_cleanup_started.set()
                await finish_request_cleanup.wait()
                return httpx.Response(
                    200,
                    headers={"content-type": "audio/wav"},
                    content=_wave_bytes(),
                    request=request,
                )
            raise AssertionError("blocking request unexpectedly completed")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        output_directory = tmp_path / "ephemeral"
        cache_directory = tmp_path / "persistent"
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            output_directory,
            _presets(),
            cache_enabled=True,
            cache_dir=cache_directory,
            client=client,
        )
        token = CancellationToken("turn_close_race")
        synthesis = asyncio.create_task(
            provider.synthesize(_job(token), segment_index=0, token=token)
        )
        await asyncio.wait_for(request_started.wait(), timeout=1)

        first_close = asyncio.create_task(provider.close())
        await asyncio.wait_for(request_cleanup_started.wait(), timeout=1)
        second_close = asyncio.create_task(provider.close())
        await asyncio.sleep(0)

        rejected = await provider.synthesize(_job(token), segment_index=1, token=token)
        assert rejected.error_code == "tts_closed"
        assert not first_close.done()
        assert not second_close.done()

        # Cancelling one waiter must not cancel the shared close operation.
        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close
        assert not second_close.done()

        finish_request_cleanup.set()
        await asyncio.wait_for(second_close, timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await synthesis
        await provider.close()

        assert list(tmp_path.rglob("*.wav")) == []
        assert list(tmp_path.rglob("*.part")) == []
        assert not cache_directory.exists()
        await client.aclose()

    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_synthesis_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        request_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            request_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await finish_cleanup.wait()
            raise AssertionError("blocking request unexpectedly completed")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _presets(),
            client=client,
        )
        token = CancellationToken("turn_repeated_cancel")
        synthesis = asyncio.create_task(
            provider.synthesize(_job(token), segment_index=0, token=token)
        )
        await asyncio.wait_for(request_started.wait(), timeout=1)

        synthesis.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        synthesis.cancel()
        await asyncio.sleep(0)
        assert not synthesis.done()

        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(synthesis, timeout=1)
        await provider.close()

        assert list(tmp_path.rglob("*.wav")) == []
        assert list(tmp_path.rglob("*.part")) == []
        await client.aclose()

    asyncio.run(scenario())


def test_cancel_settlement_failure_opens_bounded_circuit_until_owned_worker_settles(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request_started = asyncio.Event()
        release_request = asyncio.Event()
        started = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal started
            started += 1
            request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_request.wait()
                return httpx.Response(
                    200,
                    headers={"content-type": "audio/wav"},
                    content=_wave_bytes(),
                    request=request,
                )
            raise AssertionError("blocking request unexpectedly completed")

        paths = AppPaths(root=tmp_path / "private")
        registry = TempAssetRegistry(
            paths,
            minimum_scavenge_age_seconds=0.0,
            directory_security=PortableDirectorySecurity(),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            paths.temp / "audio" / "tts",
            _presets(),
            client=client,
            temp_registry=registry,
            max_owned_synthesis_tasks=1,
        )

        results: list[AudioResult] = []
        for index in range(3):
            token = CancellationToken(f"turn_cancel_timeout_{index}")
            results.append(
                await provider.synthesize(
                    _job(
                        token,
                        job_id=f"job_cancel_timeout_{index}",
                        turn_id=f"turn_cancel_timeout_{index}",
                        connect_timeout_ms=80,
                        first_byte_timeout_ms=80,
                        timeout_ms=100,
                        cancellation_timeout_ms=50,
                    ),
                    segment_index=index,
                    token=token,
                )
            )
            if index == 0:
                await asyncio.wait_for(request_started.wait(), timeout=1)

        assert [result.error_code for result in results] == ["tts_cancel_timeout"] * 3
        assert started == 1
        assert len(provider._synthesis_tasks) <= 1
        assert len(provider._synthesis_cancellations) <= 1

        closing = asyncio.create_task(provider.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release_request.set()
        await asyncio.wait_for(closing, timeout=1)

        assert provider._synthesis_tasks == set()
        assert provider._synthesis_cancellations == set()
        assert registry.entries() == ()
        assert not list(paths.temp.rglob("*.wav"))
        assert not list(paths.temp.rglob("*.part"))
        await client.aclose()

    asyncio.run(scenario())


def test_cancel_while_waiting_for_owned_worker_capacity_never_leaks_slot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        started = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal started
            started += 1
            if started == 1:
                first_started.set()
                await release_first.wait()
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=_wave_bytes(),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _presets(),
            client=client,
            max_owned_synthesis_tasks=1,
        )
        first_token = CancellationToken("capacity-first")
        first = asyncio.create_task(
            provider.synthesize(_job(first_token), segment_index=0, token=first_token)
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)

        waiting_token = CancellationToken("capacity-waiting")
        waiting = asyncio.create_task(
            provider.synthesize(
                _job(waiting_token, job_id="capacity-waiting", turn_id="capacity-waiting"),
                segment_index=1,
                token=waiting_token,
            )
        )
        await asyncio.sleep(0)
        waiting_token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=1)
        assert started == 1

        release_first.set()
        first_result = await asyncio.wait_for(first, timeout=1)
        assert first_result.success
        await provider.discard(first_result)

        final_token = CancellationToken("capacity-final")
        final_result = await provider.synthesize(
            _job(final_token, job_id="capacity-final", turn_id="capacity-final"),
            segment_index=2,
            token=final_token,
        )
        assert final_result.success
        assert started == 2
        await provider.discard(final_result)
        await provider.close()
        assert provider._synthesis_tasks == set()
        assert provider._synthesis_cancellations == set()
        await client.aclose()

    asyncio.run(scenario())


def test_close_during_cache_promotion_removes_wav_and_partial_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=_wave_bytes(),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path / "ephemeral",
            _presets(),
            cache_enabled=True,
            cache_dir=tmp_path / "persistent",
            client=client,
        )
        promotion_started = asyncio.Event()
        maintenance_calls = 0

        async def block_first_cache_maintenance() -> None:
            nonlocal maintenance_calls
            maintenance_calls += 1
            if maintenance_calls == 1:
                promotion_started.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(provider, "_cleanup_cache_locked", block_first_cache_maintenance)
        token = CancellationToken("turn_cache_close")
        synthesis = asyncio.create_task(
            provider.synthesize(_job(token), segment_index=0, token=token)
        )
        await asyncio.wait_for(promotion_started.wait(), timeout=1)

        await asyncio.wait_for(provider.close(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await synthesis

        assert list(tmp_path.rglob("*.wav")) == []
        assert list(tmp_path.rglob("*.part")) == []
        await client.aclose()

    asyncio.run(scenario())


def test_close_cleans_all_outputs_and_discard_ignores_unowned_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "audio/x-wav"},
                content=_wave_bytes(),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880", tmp_path / "audio", _presets(), client=client
        )
        token = CancellationToken("turn_test")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)
        assert result.audio_path is not None

        unowned = tmp_path / "keep.wav"
        unowned.write_bytes(b"user-data")
        await provider.discard(
            AudioResult(
                job_id="foreign",
                turn_id="foreign",
                segment_id="foreign",
                success=True,
                audio_path=unowned,
            )
        )
        assert unowned.exists()

        output = result.audio_path
        await provider.close()
        await provider.close()
        assert not output.exists()
        assert unowned.exists()
        await client.aclose()

    asyncio.run(scenario())


def test_persistent_cache_is_disabled_by_default(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=_wave_bytes(),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache_dir = tmp_path / "persistent"
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path / "ephemeral",
            _presets(),
            cache_dir=cache_dir,
            client=client,
        )
        token = CancellationToken("turn_test")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)
        await provider.discard(result)

        assert not cache_dir.exists()
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


def test_concurrent_same_key_uses_one_request_and_persists_hashed_cache(tmp_path: Path) -> None:
    async def scenario() -> None:
        request_count = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            started.set()
            await release.wait()
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=_wave_bytes(),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache_dir = tmp_path / "persistent"
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path / "ephemeral",
            _presets(),
            cache_enabled=True,
            cache_dir=cache_dir,
            client=client,
        )
        first_token = CancellationToken("turn_first")
        second_token = CancellationToken("turn_second")
        first_task = asyncio.create_task(
            provider.synthesize(
                _job(first_token, job_id="first", turn_id="first"),
                segment_index=0,
                token=first_token,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        second_task = asyncio.create_task(
            provider.synthesize(
                _job(second_token, job_id="second", turn_id="second"),
                segment_index=0,
                token=second_token,
            )
        )
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        assert request_count == 1
        assert first.audio_path == second.audio_path
        assert first.audio_path is not None
        assert first.audio_path.parent == cache_dir
        assert first.audio_path.stem.isalnum() and len(first.audio_path.stem) == 64
        assert "测试语音" not in first.audio_path.name
        await provider.discard(first)
        await provider.discard(second)
        cache_path = first.audio_path
        await provider.close()
        assert cache_path.exists()
        assert list(cache_dir.glob(".*.part")) == []
        await client.aclose()

    asyncio.run(scenario())


def test_sensitive_text_and_policy_failure_never_enter_persistent_cache(tmp_path: Path) -> None:
    async def scenario() -> None:
        request_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=_wave_bytes(),
                request=request,
            )

        def broken_policy(_text: str) -> bool:
            raise RuntimeError("policy unavailable")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache_dir = tmp_path / "persistent"
        sensitive_provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path / "ephemeral",
            _presets(),
            cache_enabled=True,
            cache_dir=cache_dir,
            sensitive_text_predicate=lambda _text: False,
            client=client,
        )
        for index in range(2):
            token = CancellationToken(f"turn_{index}")
            result = await sensitive_provider.synthesize(
                _job(
                    token,
                    job_id=f"job_{index}",
                    turn_id=f"turn_{index}",
                    text=(
                        "我的密码是 123456" if index == 0 else "联系 13812345678 或 me@example.com"
                    ),
                ),
                segment_index=index,
                token=token,
            )
            await sensitive_provider.discard(result)
        await sensitive_provider.close()

        fail_closed_provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path / "ephemeral",
            _presets(),
            cache_enabled=True,
            cache_dir=cache_dir,
            sensitive_text_predicate=broken_policy,
            client=client,
        )
        token = CancellationToken("turn_policy_failure")
        result = await fail_closed_provider.synthesize(
            _job(
                token,
                job_id="job_policy_failure",
                turn_id="turn_policy_failure",
                text="普通但策略不可用的文本",
            ),
            segment_index=2,
            token=token,
        )
        await fail_closed_provider.discard(result)

        assert request_count == 3
        assert not cache_dir.exists()
        await fail_closed_provider.close()
        await client.aclose()

    asyncio.run(scenario())


def test_cache_enforces_lru_capacity_and_ttl_without_deleting_leased_audio(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        audio = _wave_bytes()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=audio,
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache_dir = tmp_path / "persistent"
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            tmp_path / "ephemeral",
            _presets(),
            cache_enabled=True,
            cache_dir=cache_dir,
            cache_max_bytes=len(audio) * 2,
            cache_ttl_seconds=5,
            client=client,
        )

        async def synthesize(index: int) -> AudioResult:
            token = CancellationToken(f"turn_{index}")
            return await provider.synthesize(
                _job(
                    token,
                    job_id=f"job_{index}",
                    turn_id=f"turn_{index}",
                    text=f"缓存文本 {index}",
                ),
                segment_index=index,
                token=token,
            )

        first = await synthesize(1)
        second = await synthesize(2)
        assert first.audio_path is not None and second.audio_path is not None
        await provider.discard(first)
        await provider.discard(second)
        os.utime(first.audio_path, (time.time() - 2, time.time() - 2))
        os.utime(second.audio_path, (time.time() - 1, time.time() - 1))

        third = await synthesize(3)
        assert third.audio_path is not None and third.audio_path.exists()
        assert not first.audio_path.exists()
        assert second.audio_path.exists()
        # The newest entry is leased to playback and must survive capacity cleanup.
        assert third.audio_path.exists()
        await provider.discard(third)

        os.utime(second.audio_path, (time.time() - 10, time.time() - 10))
        fourth = await synthesize(4)
        assert not second.audio_path.exists()
        assert fourth.audio_path is not None and fourth.audio_path.exists()
        await provider.discard(fourth)
        assert sum(path.stat().st_size for path in cache_dir.glob("*.wav")) <= len(audio) * 2
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


def test_uncached_audio_part_and_final_are_tracked_until_discard(tmp_path: Path) -> None:
    async def scenario() -> None:
        audio = _wave_bytes()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=audio,
                request=request,
            )

        paths = AppPaths(root=tmp_path / "private")
        registry = TempAssetRegistry(
            paths,
            minimum_scavenge_age_seconds=0.0,
            directory_security=PortableDirectorySecurity(),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880",
            paths.temp / "audio" / "tts",
            _presets(),
            client=client,
            temp_registry=registry,
        )
        token = CancellationToken("turn_registry")

        result = await provider.synthesize(_job(token), segment_index=0, token=token)

        assert result.success
        assert result.audio_path is not None and result.audio_path.exists()
        entries = registry.entries()
        assert len(entries) == 1
        assert entries[0].relative_path == result.audio_path.relative_to(paths.temp).as_posix()
        assert entries[0].kind.value == "tts_wav"
        assert not tuple(result.audio_path.parent.glob("*.part"))

        await provider.discard(result)
        assert registry.entries() == ()
        assert not result.audio_path.exists()
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())


class _DelayedAudioStream(httpx.AsyncByteStream):
    def __init__(self, first_delay: float, chunks: list[bytes], *, stall_after_first: bool = False):
        self._first_delay = first_delay
        self._chunks = chunks
        self._stall_after_first = stall_after_first
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.sleep(self._first_delay)
        for index, chunk in enumerate(self._chunks):
            yield chunk
            if index == 0 and self._stall_after_first:
                await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("first_delay", "first_byte_ms", "expected"),
    [
        (0.05, 81, None),
        (0.10, 79, "tts_first_byte_timeout"),
    ],
)
def test_first_byte_7_9_and_8_1_boundary_is_owned_by_tts_job(
    tmp_path: Path,
    first_delay: float,
    first_byte_ms: int,
    expected: str | None,
) -> None:
    async def scenario() -> None:
        stream = _DelayedAudioStream(first_delay, [_wave_bytes()])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "audio/wav"}, stream=stream)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
        provider = GPTSoVITSProvider("http://127.0.0.1:9880", tmp_path, _presets(), client=client)
        token = CancellationToken("w08-first-byte")
        result = await provider.synthesize(
            _job(token, first_byte_timeout_ms=first_byte_ms),
            segment_index=0,
            token=token,
        )
        assert result.error_code == expected
        assert result.success is (expected is None)
        await provider.discard(result)
        await provider.close()
        await client.aclose()
        assert stream.closed
        assert not list(tmp_path.rglob("*.part"))

    asyncio.run(scenario())


def test_connect_and_total_deadlines_have_distinct_codes_and_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        def connect_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("synthetic", request=request)

        connect_client = httpx.AsyncClient(
            transport=httpx.MockTransport(connect_timeout), timeout=None
        )
        connect_provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880", tmp_path / "connect", _presets(), client=connect_client
        )
        connect_token = CancellationToken("w08-connect")
        connect_result = await connect_provider.synthesize(
            _job(connect_token, connect_timeout_ms=79),
            segment_index=0,
            token=connect_token,
        )
        assert connect_result.error_code == "tts_connect_timeout"
        await connect_provider.close()
        await connect_client.aclose()

        stream = _DelayedAudioStream(0, [_wave_bytes()], stall_after_first=True)

        def stalled(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "audio/wav"}, stream=stream)

        total_client = httpx.AsyncClient(transport=httpx.MockTransport(stalled), timeout=None)
        total_provider = GPTSoVITSProvider(
            "http://127.0.0.1:9880", tmp_path / "total", _presets(), client=total_client
        )
        total_token = CancellationToken("w08-total")
        total_result = await total_provider.synthesize(
            _job(total_token, first_byte_timeout_ms=80, timeout_ms=100),
            segment_index=0,
            token=total_token,
        )
        assert total_result.error_code == "tts_total_timeout"
        await total_provider.close()
        await total_client.aclose()
        assert stream.closed
        assert not list(tmp_path.rglob("*.part"))

    asyncio.run(scenario())


def test_tts_redirect_is_rejected_without_following_downgrade(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://tts.example/plaintext"})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
        provider = GPTSoVITSProvider("https://tts.example", tmp_path, _presets(), client=client)
        token = CancellationToken("w08-redirect")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)
        assert result.error_code == "tts_protocol_error"
        await provider.close()
        await client.aclose()

    asyncio.run(scenario())
    assert calls == 1
