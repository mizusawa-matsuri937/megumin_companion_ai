"""Generated-CA loopback TLS/WSS coverage for every W08 provider boundary."""

from __future__ import annotations

import asyncio
import io
import socket
import ssl
import wave
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import trustme
from app.clients.llm import LLMErrorCode, LLMProviderError, OpenAICompatibleLLMProvider
from app.clients.tts import GPTSoVITSPreset, GPTSoVITSProvider
from app.clients.vts import VTSClient, VTSConnectionError
from app.core import CancellationToken
from app.schemas import ChatMessage, ChatRequest, ChatRole, TTSJob
from websockets.asyncio.server import ServerConnection, serve


def _wave_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * 160)
    return output.getvalue()


def _server_context(cert: trustme.LeafCert) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(context)
    return context


async def _read_http_request(reader: asyncio.StreamReader) -> None:
    headers = await reader.readuntil(b"\r\n\r\n")
    content_length = 0
    for line in headers.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1])
    if content_length:
        await reader.readexactly(content_length)


async def _with_https_server(
    context: ssl.SSLContext,
    response_factory: Callable[[], bytes],
    action: Callable[[int], Awaitable[None]],
) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _read_http_request(reader)
            writer.write(response_factory())
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=context)
    assert server.sockets
    port = int(server.sockets[0].getsockname()[1])
    try:
        await action(port)
    finally:
        server.close()
        await server.wait_closed()


def test_generated_custom_ca_allows_llm_and_tts_without_disabling_verification(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ca = trustme.CA()
        cert = ca.issue_cert("localhost")
        ca_path = tmp_path / "synthetic-ca.pem"
        ca.cert_pem.write_to_path(ca_path)

        sse = (
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )

        def llm_response() -> bytes:
            return (
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                + str(len(sse)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + sse
            )

        async def use_llm(port: int) -> None:
            provider = OpenAICompatibleLLMProvider(
                base_url=f"https://localhost:{port}",
                model="synthetic-model",
                api_key="synthetic-key",
                ca_bundle_path=ca_path,
            )
            request = ChatRequest(messages=[ChatMessage(role=ChatRole.user, content="synthetic")])
            try:
                assert [
                    delta async for delta in provider.stream(request, CancellationToken("tls-llm"))
                ] == ["ok"]
            finally:
                await provider.close()

        await _with_https_server(_server_context(cert), llm_response, use_llm)

        wav = _wave_bytes()

        def tts_response() -> bytes:
            return (
                b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nContent-Length: "
                + str(len(wav)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + wav
            )

        async def use_tts(port: int) -> None:
            provider = GPTSoVITSProvider(
                f"https://localhost:{port}",
                tmp_path / "audio",
                {"default": GPTSoVITSPreset(ref_audio_path="/synthetic/reference.wav")},
                ca_bundle_path=ca_path,
            )
            token = CancellationToken("tls-tts")
            job = TTSJob(
                turn_id="turn",
                segment_id="segment",
                text="synthetic",
                connect_timeout_ms=1000,
                first_byte_timeout_ms=1000,
                timeout_ms=3000,
                cancellation_timeout_ms=500,
                cancellation_token_id=token.token_id,
            )
            result = await provider.synthesize(job, segment_index=0, token=token)
            assert result.success
            await provider.discard(result)
            await provider.close()

        await _with_https_server(_server_context(cert), tts_response, use_tts)
        assert not list(tmp_path.rglob("*.part"))

    asyncio.run(scenario())


@pytest.mark.parametrize("certificate_case", ["untrusted", "hostname", "expired"])
def test_untrusted_hostname_mismatch_and_expired_certificates_fail_closed(
    tmp_path: Path, certificate_case: str
) -> None:
    async def scenario() -> None:
        trusted_ca = trustme.CA()
        server_ca = trustme.CA() if certificate_case == "untrusted" else trusted_ca
        identity = "wrong.example" if certificate_case == "hostname" else "localhost"
        now = datetime.now(UTC)
        cert = server_ca.issue_cert(
            identity,
            not_before=now - timedelta(days=3),
            not_after=(now - timedelta(days=1) if certificate_case == "expired" else None),
        )
        ca_path = tmp_path / "synthetic-trust.pem"
        trusted_ca.cert_pem.write_to_path(ca_path)
        body = b"data: [DONE]\n\n"

        def response() -> bytes:
            return (
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )

        async def connect(port: int) -> None:
            provider = OpenAICompatibleLLMProvider(
                base_url=f"https://localhost:{port}",
                model="synthetic-model",
                api_key="synthetic-key",
                ca_bundle_path=ca_path,
            )
            request = ChatRequest(messages=[ChatMessage(role=ChatRole.user, content="synthetic")])
            try:
                with pytest.raises(LLMProviderError) as captured:
                    _ = [
                        delta
                        async for delta in provider.stream(request, CancellationToken("tls-fail"))
                    ]
                assert captured.value.code is LLMErrorCode.connection
                assert str(ca_path) not in str(captured.value)
                assert "synthetic" not in str(captured.value)
            finally:
                await provider.close()

        await _with_https_server(_server_context(cert), response, connect)

        async def connect_tts(port: int) -> None:
            provider = GPTSoVITSProvider(
                f"https://localhost:{port}",
                tmp_path / "failed-audio",
                {"default": GPTSoVITSPreset(ref_audio_path="/synthetic/reference.wav")},
                ca_bundle_path=ca_path,
            )
            token = CancellationToken("tls-tts-fail")
            job = TTSJob(
                turn_id="turn",
                segment_id="segment",
                text="synthetic",
                connect_timeout_ms=1000,
                first_byte_timeout_ms=1000,
                timeout_ms=3000,
                cancellation_timeout_ms=500,
                cancellation_token_id=token.token_id,
            )
            try:
                result = await provider.synthesize(job, segment_index=0, token=token)
                assert result.error_code == "tts_tls_error"
                assert str(ca_path) not in repr(result)
                assert not list((tmp_path / "failed-audio").rglob("*.part"))
            finally:
                await provider.close()

        await _with_https_server(_server_context(cert), response, connect_tts)

    asyncio.run(scenario())


def test_generated_ca_wss_succeeds_and_untrusted_wss_fails(tmp_path: Path) -> None:
    async def scenario() -> None:
        ca = trustme.CA()
        cert = ca.issue_cert("localhost")
        ca_path = tmp_path / "synthetic-vts-ca.pem"
        ca.cert_pem.write_to_path(ca_path)

        async def handler(connection: ServerConnection) -> None:
            await connection.wait_closed()

        server = await serve(handler, "127.0.0.1", 0, ssl=_server_context(cert))
        assert server.sockets
        port = int(server.sockets[0].getsockname()[1])
        try:
            trusted = VTSClient(f"wss://localhost:{port}", ca_bundle_path=ca_path)
            await trusted.connect()
            assert trusted.connected
            await trusted.close()

            untrusted = VTSClient(f"wss://localhost:{port}")
            with pytest.raises(VTSConnectionError):
                await untrusted.connect()
            await untrusted.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_process_proxy_is_ignored_and_explicit_proxy_failure_is_content_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        ca = trustme.CA()
        cert = ca.issue_cert("localhost")
        ca_path = tmp_path / "synthetic-proxy-ca.pem"
        ca.cert_pem.write_to_path(ca_path)
        body = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

        def response() -> bytes:
            return (
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            dead_proxy_port = int(reservation.getsockname()[1])
        dead_proxy = f"http://127.0.0.1:{dead_proxy_port}"
        monkeypatch.setenv("HTTPS_PROXY", dead_proxy)
        monkeypatch.setenv("HTTP_PROXY", dead_proxy)

        async def connect(port: int) -> None:
            request = ChatRequest(messages=[ChatMessage(role=ChatRole.user, content="synthetic")])
            direct = OpenAICompatibleLLMProvider(
                base_url=f"https://localhost:{port}",
                model="synthetic-model",
                api_key="synthetic-key",
                ca_bundle_path=ca_path,
            )
            assert [
                delta async for delta in direct.stream(request, CancellationToken("direct"))
            ] == []
            await direct.close()

            proxied = OpenAICompatibleLLMProvider(
                base_url=f"https://localhost:{port}",
                model="synthetic-model",
                api_key="synthetic-key",
                ca_bundle_path=ca_path,
                proxy_url=dead_proxy,
            )
            try:
                with pytest.raises(LLMProviderError) as captured:
                    _ = [
                        delta async for delta in proxied.stream(request, CancellationToken("proxy"))
                    ]
                assert captured.value.code is LLMErrorCode.connection
                assert dead_proxy not in str(captured.value)
                assert str(ca_path) not in str(captured.value)
            finally:
                await proxied.close()

        await _with_https_server(_server_context(cert), response, connect)

    asyncio.run(scenario())


def test_vts_rejects_websocket_redirect_without_contacting_target() -> None:
    async def scenario() -> None:
        target_connections = 0

        async def target_handler(connection: ServerConnection) -> None:
            nonlocal target_connections
            target_connections += 1
            await connection.wait_closed()

        target = await serve(target_handler, "127.0.0.1", 0)
        assert target.sockets
        target_port = int(target.sockets[0].getsockname()[1])

        async def redirect_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:"
                    + str(target_port).encode()
                    + b"/redirected\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        redirect = await asyncio.start_server(redirect_handler, "127.0.0.1", 0)
        assert redirect.sockets
        redirect_port = int(redirect.sockets[0].getsockname()[1])
        client = VTSClient(f"ws://127.0.0.1:{redirect_port}")
        try:
            with pytest.raises(VTSConnectionError):
                await client.connect()
            assert target_connections == 0
        finally:
            await client.close()
            redirect.close()
            await redirect.wait_closed()
            target.close()
            await target.wait_closed()

    asyncio.run(scenario())
