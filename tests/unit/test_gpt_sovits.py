"""Offline protocol and filesystem tests for the GPT-SoVITS adapter."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from app.clients.tts.gpt_sovits import GPTSoVITSPreset, GPTSoVITSProvider
from app.core import CancellationToken
from app.schemas import AudioResult, TTSJob


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
    ("status", "available", "error_code"),
    [(422, True, None), (404, False, "tts_protocol_error"), (503, False, "tts_unavailable")],
)
def test_probe_classifies_api_v2_without_synthesizing(
    tmp_path: Path, status: int, available: bool, error_code: str | None
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL("http://gpt-sovits.local/tts")
            assert json.loads(request.content) == {}
            return httpx.Response(status, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GPTSoVITSProvider("http://gpt-sovits.local", tmp_path, _presets(), client=client)
        result = await provider.probe()

        assert result.available is available
        assert result.protocol == ("api_v2" if available else None)
        assert result.error_code == error_code
        await provider.close()
        assert not client.is_closed
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
        provider = GPTSoVITSProvider("http://gpt-sovits.local", tmp_path, _presets(), client=client)
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
        provider = GPTSoVITSProvider("http://gpt-sovits.local", tmp_path, _presets(), client=client)
        token = CancellationToken("turn_test")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)

        assert not result.success
        assert result.error_code == error_code
        assert list(tmp_path.rglob("*")) == []
        await provider.close()
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
            "http://gpt-sovits.local",
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
        provider = GPTSoVITSProvider("http://gpt-sovits.local", tmp_path, _presets(), client=client)
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
            "http://gpt-sovits.local", tmp_path / "audio", _presets(), client=client
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
