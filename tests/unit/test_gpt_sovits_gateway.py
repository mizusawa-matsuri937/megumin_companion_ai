"""Path-free tests for the authenticated W30 Gateway compatibility adapter."""

from __future__ import annotations

import asyncio
import io
import json
import ssl
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from app.clients.tts import GPTSoVITSGatewayProvider
from app.clients.tts import gateway as gateway_module
from app.core import CancellationToken
from app.limits import LimitsConfig
from app.schemas import AudioResult, TTSJob
from app.temp_assets import TempAssetKind, TempAssetRegistry, TempRegistryError

_TOKEN = "A" * 43


def _wave_bytes(*, sample_rate: int = 32_000, frame_count: int = 320) -> bytes:
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
        "text": "固定中文测试句",
        # Deliberately keep W30's direct-provider style vocabulary here. Gateway
        # routing must select from the bounded emotion label instead.
        "style": "default",
        "emotion": "worried",
        "speed_factor": 1.05,
        "connect_timeout_ms": 80,
        "first_byte_timeout_ms": 80,
        "timeout_ms": 300,
        "cancellation_timeout_ms": 80,
        "cancellation_token_id": token.token_id,
    }
    values.update(changes)
    return TTSJob.model_validate(values)


def test_gateway_provider_probe_synthesize_discard_and_minimal_payload(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.headers["authorization"] == f"Bearer {_TOKEN}"
            assert request.headers["x-tts-gateway-protocol"] == "1"
            assert request.url.host == "127.0.0.1"
            if request.url.path == "/v1/health":
                return httpx.Response(
                    200,
                    headers={"X-TTS-Gateway-Protocol": "1"},
                    json={
                        "protocol_version": 1,
                        "status": "ready",
                        "current_slot": "neutral",
                    },
                )
            assert request.url.path == "/v1/tts"
            assert json.loads(request.content) == {
                "text": "固定中文测试句",
                "voice_slot": "gentle",
                "speed_factor": 1.05,
            }
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "audio/wav",
                    "X-TTS-Gateway-Protocol": "1",
                },
                content=_wave_bytes(),
            )

        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path / "audio",
            _TOKEN,
            transport=httpx.MockTransport(handler),
        )
        probe = await provider.probe(timeout_ms=100)
        assert probe.available
        assert probe.protocol == "gateway_v1"

        token = CancellationToken("turn")
        result = await provider.synthesize(_job(token), segment_index=2, token=token)
        assert result.success
        assert result.sample_rate == 32_000
        assert result.duration_ms == 10
        assert result.audio_path is not None and result.audio_path.is_file()
        assert ".." not in result.audio_path.relative_to(tmp_path).parts
        await provider.discard(result)
        assert not result.audio_path.exists()
        await provider.close()
        assert len(requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:9880",
        "http://127.0.0.1:9881",
        "https://127.0.0.1:9880",
        "http://127.0.0.1:9880/path",
    ],
)
def test_gateway_provider_rejects_any_noncanonical_endpoint(
    tmp_path: Path,
    base_url: str,
) -> None:
    with pytest.raises(ValueError):
        GPTSoVITSGatewayProvider(base_url, tmp_path, _TOKEN)


def test_gateway_provider_rejects_invalid_slot_without_network(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            transport=httpx.MockTransport(handler),
        )
        token = CancellationToken("turn")
        result = await provider.synthesize(
            _job(token, emotion="private-model-path"),
            segment_index=0,
            token=token,
        )
        assert not result.success
        assert result.error_code == "tts_voice_slot_invalid"
        assert calls == 0
        await provider.close()

    asyncio.run(scenario())


def test_gateway_provider_rejects_invalid_constructor_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        GPTSoVITSGatewayProvider("http://127.0.0.1:9880", tmp_path, "short")
    with pytest.raises(ValueError):
        GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            max_audio_bytes=43,
        )
    with pytest.raises(ValueError):
        GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            max_owned_synthesis_tasks=0,
        )
    with pytest.raises(ValueError):
        GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            max_owned_synthesis_tasks=LimitsConfig().tts_queue_capacity + 1,
        )


def test_gateway_provider_probe_failure_matrix(tmp_path: Path) -> None:
    async def scenario() -> None:
        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        def unavailable_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("synthetic unavailable", request=request)

        cases = (
            (
                lambda _request: httpx.Response(
                    200,
                    headers={"X-TTS-Gateway-Protocol": "1"},
                    content=b"not-json",
                ),
                "tts_protocol_error",
            ),
            (
                lambda _request: httpx.Response(
                    200,
                    headers={"X-TTS-Gateway-Protocol": "1"},
                    json={
                        "protocol_version": 1,
                        "status": "quarantine",
                        "current_slot": None,
                    },
                ),
                "tts_protocol_error",
            ),
            (lambda _request: httpx.Response(401), "tts_auth_failed"),
            (
                lambda _request: httpx.Response(
                    200,
                    headers={"X-TTS-Gateway-Protocol": "1"},
                    content=b"x" * 4097,
                ),
                "tts_protocol_error",
            ),
            (timeout_handler, "tts_timeout"),
            (unavailable_handler, "tts_unavailable"),
        )
        for index, (handler, expected) in enumerate(cases):
            provider = GPTSoVITSGatewayProvider(
                "http://127.0.0.1:9880",
                tmp_path / str(index),
                _TOKEN,
                transport=httpx.MockTransport(handler),
            )
            result = await provider.probe(timeout_ms=20)
            assert not result.available
            assert result.error_code == expected
            await provider.close()

        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path / "closed",
            _TOKEN,
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )
        with pytest.raises(ValueError):
            await provider.probe(timeout_ms=0)
        await provider.close()
        assert (await provider.probe(timeout_ms=20)).error_code == "tts_closed"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("headers", "content", "expected"),
    [
        ({"Content-Type": "audio/wav"}, _wave_bytes(), "tts_protocol_error"),
        (
            {
                "Content-Type": "application/json",
                "X-TTS-Gateway-Protocol": "1",
            },
            b'{"private_path":"must-not-leak"}',
            "tts_invalid_content_type",
        ),
        (
            {
                "Content-Type": "audio/wav",
                "X-TTS-Gateway-Protocol": "1",
            },
            b"not-a-wave",
            "tts_invalid_audio",
        ),
    ],
)
def test_gateway_provider_rejects_protocol_or_audio_mismatch_without_leak(
    tmp_path: Path,
    headers: dict[str, str],
    content: bytes,
    expected: str,
) -> None:
    async def scenario() -> None:
        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, headers=headers, content=content)
            ),
        )
        token = CancellationToken("turn")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)
        assert not result.success
        assert result.error_code == expected
        assert "private_path" not in str(result)
        assert list(tmp_path.rglob("*.wav")) == []
        await provider.close()

    asyncio.run(scenario())


class _BlockedStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.release.wait()
        yield _wave_bytes()

    async def aclose(self) -> None:
        self.release.set()


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], *, delay: float = 0.0) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._delay:
            await asyncio.sleep(self._delay)
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def test_gateway_provider_cancellation_removes_partial_audio(tmp_path: Path) -> None:
    async def scenario() -> None:
        stream = _BlockedStream()
        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={
                        "Content-Type": "audio/wav",
                        "X-TTS-Gateway-Protocol": "1",
                    },
                    stream=stream,
                )
            ),
        )
        token = CancellationToken("turn")
        task = asyncio.create_task(provider.synthesize(_job(token), segment_index=0, token=token))
        await stream.started.wait()
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.rglob("*.wav")) == []
        assert list(tmp_path.rglob("*.part")) == []
        await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("auth", "tts_auth_failed"),
        ("declared_large", "tts_response_too_large"),
        ("stream_large", "tts_response_too_large"),
        ("empty", "tts_invalid_audio"),
        ("first_byte", "tts_first_byte_timeout"),
        ("connect_timeout", "tts_connect_timeout"),
        ("read_timeout", "tts_total_timeout"),
        ("connection", "tts_connection_error"),
    ],
)
def test_gateway_provider_maps_bounded_transport_failures(
    tmp_path: Path,
    kind: str,
    expected: str,
) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            headers = {
                "Content-Type": "audio/wav",
                "X-TTS-Gateway-Protocol": "1",
            }
            if kind == "auth":
                return httpx.Response(403)
            if kind == "declared_large":
                return httpx.Response(
                    200,
                    headers={**headers, "Content-Length": "65"},
                    stream=_ChunkStream(()),
                )
            if kind == "stream_large":
                return httpx.Response(
                    200,
                    headers=headers,
                    stream=_ChunkStream((b"x" * 40, b"y" * 40)),
                )
            if kind == "empty":
                return httpx.Response(
                    200,
                    headers=headers,
                    stream=_ChunkStream((b"",)),
                )
            if kind == "first_byte":
                return httpx.Response(
                    200,
                    headers=headers,
                    stream=_ChunkStream((_wave_bytes(),), delay=0.1),
                )
            if kind == "connect_timeout":
                raise httpx.ConnectTimeout("synthetic connect timeout", request=request)
            if kind == "read_timeout":
                raise httpx.ReadTimeout("synthetic read timeout", request=request)
            raise httpx.ConnectError("synthetic connection failure", request=request)

        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            max_audio_bytes=64,
            transport=httpx.MockTransport(handler),
        )
        token = CancellationToken("turn")
        result = await provider.synthesize(
            _job(token, first_byte_timeout_ms=20 if kind == "first_byte" else 200),
            segment_index=0,
            token=token,
        )
        assert not result.success
        assert result.error_code == expected
        assert not list(tmp_path.rglob("*.part"))
        await provider.close()

    asyncio.run(scenario())


def test_gateway_provider_capacity_close_and_total_timeout_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path / "capacity",
            _TOKEN,
            max_owned_synthesis_tasks=1,
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )
        await provider._synthesis_capacity.acquire()
        token = CancellationToken("turn")
        result = await provider.synthesize(
            _job(
                token,
                timeout_ms=20,
                connect_timeout_ms=10,
                first_byte_timeout_ms=10,
                cancellation_timeout_ms=10,
            ),
            segment_index=0,
            token=token,
        )
        assert result.error_code == "tts_total_timeout"
        provider._synthesis_capacity.release()

        blocker = asyncio.create_task(asyncio.sleep(0))
        provider._synthesis_cancellations.add(cast(asyncio.Task[AudioResult], blocker))
        assert (
            await provider.synthesize(_job(token), segment_index=0, token=token)
        ).error_code == "tts_cancel_timeout"
        provider._synthesis_cancellations.clear()
        await blocker

        monkeypatch.setattr(provider, "_is_closed", lambda: True)
        assert (
            await provider.synthesize(_job(token), segment_index=0, token=token)
        ).error_code == "tts_closed"
        await provider.close()
        assert (
            await provider.synthesize(_job(token), segment_index=0, token=token)
        ).error_code == "tts_closed"

        stream = _BlockedStream()
        timed = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path / "timed",
            _TOKEN,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={
                        "Content-Type": "audio/wav",
                        "X-TTS-Gateway-Protocol": "1",
                    },
                    stream=stream,
                )
            ),
        )
        timed_token = CancellationToken("timed-turn")
        timed_result = await timed.synthesize(
            _job(
                timed_token,
                timeout_ms=300,
                connect_timeout_ms=200,
                first_byte_timeout_ms=300,
                cancellation_timeout_ms=300,
            ),
            segment_index=0,
            token=timed_token,
        )
        assert timed_result.error_code == "tts_total_timeout"
        await timed.close()

    asyncio.run(scenario())


def test_gateway_provider_close_cancels_an_active_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        stream = _BlockedStream()
        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={
                        "Content-Type": "audio/wav",
                        "X-TTS-Gateway-Protocol": "1",
                    },
                    stream=stream,
                )
            ),
        )
        token = CancellationToken("active-close")
        synthesis = asyncio.create_task(
            provider.synthesize(
                _job(
                    token,
                    timeout_ms=1000,
                    first_byte_timeout_ms=1000,
                    cancellation_timeout_ms=1000,
                ),
                segment_index=0,
                token=token,
            )
        )
        await stream.started.wait()
        await provider.close()
        outcome = (await asyncio.gather(synthesis, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        assert not list(tmp_path.rglob("*.part"))

    asyncio.run(scenario())


class _RegistryEntry:
    asset_id = "asset_123"


class _Registry:
    def __init__(self) -> None:
        self.registered: list[tuple[Path, TempAssetKind]] = []
        self.moved: list[tuple[str, Path, TempAssetKind]] = []
        self.deleted: list[tuple[str, bool]] = []

    def register(self, path: Path, kind: TempAssetKind) -> _RegistryEntry:
        self.registered.append((path, kind))
        return _RegistryEntry()

    def mark_moved(self, asset_id: str, path: Path, *, kind: TempAssetKind) -> None:
        self.moved.append((asset_id, path, kind))

    def delete(self, asset_id: str, *, ignore_retry_deadline: bool = False) -> None:
        self.deleted.append((asset_id, ignore_retry_deadline))


def test_gateway_provider_uses_temp_registry_for_atomic_wav_lifecycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _Registry()
        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            temp_registry=cast(TempAssetRegistry, registry),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={
                        "Content-Type": "audio/wav",
                        "X-TTS-Gateway-Protocol": "1",
                    },
                    content=_wave_bytes(),
                )
            ),
        )
        token = CancellationToken("turn")
        result = await provider.synthesize(_job(token), segment_index=0, token=token)
        assert result.success
        await provider.discard(result)
        await provider.discard(result)
        await provider.close()
        assert [kind for _path, kind in registry.registered] == [TempAssetKind.tts_part]
        assert [kind for _asset_id, _path, kind in registry.moved] == [TempAssetKind.tts_wav]
        assert registry.deleted == [("asset_123", True)]

    asyncio.run(scenario())


def test_gateway_provider_maps_temp_registry_and_tls_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path / "registry",
            _TOKEN,
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )

        def reject_registry(_path: Path, _kind: TempAssetKind) -> None:
            raise TempRegistryError("synthetic_registry_failure")

        monkeypatch.setattr(provider, "_register_temp_path", reject_registry)
        token = CancellationToken("turn")
        assert (
            await provider.synthesize(_job(token), segment_index=0, token=token)
        ).error_code == "tts_temp_registry_failed"
        await provider.close()

        def tls_handler(request: httpx.Request) -> httpx.Response:
            try:
                raise ssl.SSLError("synthetic TLS failure")
            except ssl.SSLError as cause:
                raise httpx.ConnectError("wrapped TLS failure", request=request) from cause

        tls_provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path / "tls",
            _TOKEN,
            transport=httpx.MockTransport(tls_handler),
        )
        tls_token = CancellationToken("tls-turn")
        assert (
            await tls_provider.synthesize(
                _job(tls_token),
                segment_index=0,
                token=tls_token,
            )
        ).error_code == "tts_tls_error"
        await tls_provider.close()

    asyncio.run(scenario())


def test_gateway_client_helpers_cover_cancellation_and_validation(tmp_path: Path) -> None:
    async def scenario() -> None:
        response = httpx.Response(
            200,
            stream=_ChunkStream((b"12", b"34")),
        )
        assert await gateway_module._read_bounded(response, 4) == b"1234"
        with pytest.raises(ValueError):
            await gateway_module._read_bounded(
                httpx.Response(200, stream=_ChunkStream((b"12345",))),
                4,
            )

        token = CancellationToken("helper-turn")
        assert await gateway_module._await_with_token(asyncio.sleep(0, result=7), token) == 7
        cancelled = CancellationToken("cancelled-helper-turn")
        wait_task = asyncio.create_task(
            gateway_module._await_with_token(asyncio.sleep(10), cancelled)
        )
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait_task

        semaphore = asyncio.Semaphore(1)
        assert await gateway_module._acquire_semaphore_with_token(
            semaphore,
            CancellationToken("acquire"),
            timeout_seconds=0.1,
        )
        assert not await gateway_module._acquire_semaphore_with_token(
            semaphore,
            CancellationToken("timeout"),
            timeout_seconds=0,
        )
        semaphore.release()

        release = asyncio.Event()

        async def wait_for_release() -> int:
            await release.wait()
            return 3

        cleanup_task = asyncio.create_task(gateway_module._finish_cleanup(wait_for_release()))
        await asyncio.sleep(0)
        cleanup_task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task

        assert await gateway_module._join_task(
            asyncio.create_task(asyncio.sleep(0)),
            0.1,
        )
        pending = asyncio.create_task(asyncio.sleep(10))
        assert not await gateway_module._join_task(pending, 0.01)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

        async def fail() -> None:
            raise RuntimeError("synthetic helper failure")

        assert await gateway_module._join_task(asyncio.create_task(fail()), 0.1)

        blocked_semaphore = asyncio.Semaphore(0)
        cancelled_acquisition = CancellationToken("cancelled-acquisition")
        acquisition = asyncio.create_task(
            gateway_module._acquire_semaphore_with_token(
                blocked_semaphore,
                cancelled_acquisition,
                timeout_seconds=0.1,
            )
        )
        await asyncio.sleep(0)
        cancelled_acquisition.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquisition

    asyncio.run(scenario())

    assert gateway_module._status_error(401) == "tts_auth_failed"
    assert gateway_module._status_error(429) == "tts_rate_limited"
    assert gateway_module._status_error(408) == "tts_timeout"
    assert gateway_module._status_error(500) == "tts_unavailable"
    assert gateway_module._status_error(400) == "tts_protocol_error"

    assert gateway_module._content_length(httpx.Response(200)) is None
    assert (
        gateway_module._content_length(httpx.Response(200, headers={"Content-Length": "invalid"}))
        is None
    )
    assert (
        gateway_module._content_length(httpx.Response(200, headers={"Content-Length": "-1"}))
        is None
    )
    assert (
        gateway_module._content_length(httpx.Response(200, headers={"Content-Length": "12"})) == 12
    )

    with pytest.raises(gateway_module._AudioValidationError):
        gateway_module._inspect_wave(tmp_path / "missing.wav")
    invalid = tmp_path / "invalid.wav"
    with wave.open(str(invalid), "wb") as audio:
        audio.setnchannels(3)
        audio.setsampwidth(2)
        audio.setframerate(32_000)
        audio.writeframes(b"\0" * 6)
    with pytest.raises(gateway_module._AudioValidationError):
        gateway_module._inspect_wave(invalid)

    assert gateway_module._has_ssl_error(ssl.SSLError("direct"))
    assert not gateway_module._has_ssl_error(ValueError("plain"))


@pytest.mark.parametrize(
    ("emotion", "expected_slot"),
    [
        ("neutral", "neutral"),
        ("bored", "neutral"),
        ("sleepy", "neutral"),
        ("happy", "gentle"),
        ("worried", "gentle"),
        ("shy", "tsundere"),
        ("angry_cute", "tsundere"),
        ("proud", "focused"),
        ("focused", "focused"),
        ("excited", "excited_explosion"),
        ("explosion_mode", "excited_explosion"),
        # Already-normalized gateway slots remain valid without touching the direct
        # provider's independent style field.
        ("gentle", "gentle"),
        ("tsundere", "tsundere"),
        ("excited_explosion", "excited_explosion"),
    ],
)
def test_gateway_provider_maps_w30_emotions_to_bounded_slots(
    tmp_path: Path,
    emotion: str,
    expected_slot: str,
) -> None:
    async def scenario() -> None:
        payloads: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(cast(dict[str, object], json.loads(request.content)))
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "audio/wav",
                    "X-TTS-Gateway-Protocol": "1",
                },
                content=_wave_bytes(),
            )

        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            transport=httpx.MockTransport(handler),
        )
        token = CancellationToken("emotion-map")
        result = await provider.synthesize(
            _job(token, style="bright", emotion=emotion),
            segment_index=0,
            token=token,
        )
        assert result.success
        assert payloads == [
            {
                "text": "固定中文测试句",
                "voice_slot": expected_slot,
                "speed_factor": 1.05,
            }
        ]
        await provider.close()

    asyncio.run(scenario())


def test_gateway_provider_releases_owned_capacity_after_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "audio/wav",
                    "X-TTS-Gateway-Protocol": "1",
                },
                content=_wave_bytes(),
            )

        provider = GPTSoVITSGatewayProvider(
            "http://127.0.0.1:9880",
            tmp_path,
            _TOKEN,
            max_owned_synthesis_tasks=1,
            transport=httpx.MockTransport(handler),
        )
        token = CancellationToken("capacity-release")
        first = await provider.synthesize(_job(token), segment_index=0, token=token)
        second = await provider.synthesize(_job(token), segment_index=1, token=token)
        assert first.success and second.success
        assert calls == 2
        await provider.close()

    asyncio.run(scenario())
