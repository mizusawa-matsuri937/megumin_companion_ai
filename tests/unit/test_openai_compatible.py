"""OpenAI-compatible payload, streaming, errors, and lifecycle tests."""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from app.clients.llm import (
    LLMErrorCode,
    LLMProviderError,
    OpenAICompatibleLLMProvider,
    StreamCompletionMode,
)
from app.core import CancellationToken
from app.schemas import (
    ChatMessage,
    ChatRequest,
    ChatRole,
    ImageURLContent,
    TextContent,
)


class FragmentedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            await asyncio.sleep(0)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class BlockingByteStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event) -> None:
        self._started = started
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self._started.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed = True


def make_provider(
    handler: httpx.MockTransport,
    *,
    completion_mode: StreamCompletionMode = StreamCompletionMode.done,
    max_stream_event_bytes: int = 64 * 1024,
) -> OpenAICompatibleLLMProvider:
    return OpenAICompatibleLLMProvider(
        base_url="https://provider.invalid",
        model="test-model",
        api_key="fake-test-key",
        stream_completion_mode=completion_mode,
        max_stream_event_bytes=max_stream_event_bytes,
        transport=handler,
    )


def request() -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role=ChatRole.user, content="你好")],
        temperature=0.2,
        max_tokens=50,
    )


def test_constructor_rejects_duplicate_test_transport_and_budget_bypass() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    client = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(ValueError, match="one owner"):
            OpenAICompatibleLLMProvider(
                base_url="https://provider.invalid",
                model="test-model",
                api_key="fake-test-key",
                client=client,
                transport=transport,
            )
        with pytest.raises(ValueError, match="W07 output budget"):
            OpenAICompatibleLLMProvider(
                base_url="https://provider.invalid",
                model="test-model",
                api_key="fake-test-key",
                max_stream_event_bytes=0,
            )
    finally:
        asyncio.run(client.aclose())


def test_explicit_proxy_is_validated_before_http_client_construction() -> None:
    with pytest.raises(ValueError, match="provider_proxy_invalid"):
        OpenAICompatibleLLMProvider(
            base_url="https://provider.invalid",
            model="test-model",
            api_key="fake-test-key",
            proxy_url="http://user:secret@proxy.invalid:8080",
        )


def test_stream_parses_unicode_sse_and_sends_expected_payload() -> None:
    seen: dict[str, object] = {}

    def handler(incoming: httpx.Request) -> httpx.Response:
        seen.update(json.loads(incoming.content))
        body = "".join(
            [
                ": keepalive\n\n",
                'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"你"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"好！"}}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def scenario() -> list[str]:
        provider = make_provider(httpx.MockTransport(handler))
        output = [delta async for delta in provider.stream(request(), CancellationToken("turn"))]
        await provider.close()
        await provider._client.aclose()
        return output

    assert asyncio.run(scenario()) == ["你", "好！"]
    assert seen["model"] == "test-model"
    assert seen["stream"] is True
    assert seen["messages"] == [{"role": "user", "content": "你好"}]


def test_stream_reassembles_unicode_sse_split_at_every_byte_boundary() -> None:
    body = (
        ': keepalive\n\ndata: {"choices":[{"delta":{"content":"你🌋好"}}]}\n\ndata: [DONE]\n\n'
    ).encode()
    stream = FragmentedByteStream([body[index : index + 1] for index in range(len(body))])

    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "text/event-stream; charset=utf-8"},
        )

    async def scenario() -> list[str]:
        provider = make_provider(httpx.MockTransport(handler))
        output = [delta async for delta in provider.stream(request(), CancellationToken("turn"))]
        await provider._client.aclose()
        return output

    assert asyncio.run(scenario()) == ["你🌋好"]
    assert stream.closed


def test_token_cancellation_interrupts_a_blocked_stream_body_and_closes_it() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        stream = BlockingByteStream(started)

        def handler(_incoming: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/event-stream"},
            )

        provider = make_provider(httpx.MockTransport(handler))
        token = CancellationToken("turn")

        async def consume() -> list[str]:
            return [delta async for delta in provider.stream(request(), token)]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert stream.closed
        await provider._client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["stream", "complete"])
def test_token_cancellation_interrupts_a_request_waiting_for_headers(operation: str) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def handler(_incoming: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        provider = make_provider(httpx.MockTransport(handler))
        token = CancellationToken("turn")

        async def invoke() -> object:
            if operation == "stream":
                return [delta async for delta in provider.stream(request(), token)]
            return await provider.complete(request(), token)

        task = asyncio.create_task(invoke())
        await asyncio.wait_for(started.wait(), timeout=1)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        await provider._client.aclose()

    asyncio.run(scenario())


def test_complete_returns_text_usage_and_json_mode() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "ignored": "x"},
            },
        )

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler))
        completion = await provider.complete(
            request().model_copy(update={"response_format": "json_object"}),
            CancellationToken("turn"),
        )
        assert completion.text == '{"ok":true}'
        assert completion.usage == {"prompt_tokens": 2, "completion_tokens": 3}
        await provider._client.aclose()

    asyncio.run(scenario())


def test_configured_generation_defaults_apply_and_request_values_win() -> None:
    seen: list[dict[str, object]] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        seen.append(json.loads(incoming.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    async def scenario() -> None:
        client = httpx.AsyncClient(
            base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
        )
        provider = OpenAICompatibleLLMProvider(
            base_url="https://provider.invalid",
            model="test-model",
            api_key="fake-test-key",
            default_temperature=0.8,
            default_max_tokens=600,
            client=client,
        )
        without_overrides = request().model_copy(update={"temperature": None, "max_tokens": None})
        await provider.complete(without_overrides, CancellationToken("turn-defaults"))
        await provider.complete(request(), CancellationToken("turn-overrides"))
        await client.aclose()

    asyncio.run(scenario())

    assert seen[0]["temperature"] == 0.8
    assert seen[0]["max_tokens"] == 600
    assert seen[1]["temperature"] == 0.2
    assert seen[1]["max_tokens"] == 50


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, LLMErrorCode.authentication, False),
        (429, LLMErrorCode.rate_limited, True),
        (503, LLMErrorCode.unavailable, True),
        (400, LLMErrorCode.rejected, False),
    ],
)
def test_http_errors_are_mapped_without_remote_body(
    status: int, code: LLMErrorCode, retryable: bool
) -> None:
    secret_body = "private-prompt@example.com"

    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=secret_body)

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler))
        with pytest.raises(LLMProviderError) as captured:
            await provider.complete(request(), CancellationToken("turn"))
        assert captured.value.code is code
        assert captured.value.retryable is retryable
        assert secret_body not in str(captured.value)
        await provider._client.aclose()

    asyncio.run(scenario())


def test_malformed_stream_and_pre_cancel_are_not_silently_accepted() -> None:
    def malformed(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: not-json\n\n")

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(malformed))
        with pytest.raises(LLMProviderError, match="llm_protocol_error"):
            _ = [delta async for delta in provider.stream(request(), CancellationToken("turn"))]

        token = CancellationToken("turn-cancelled")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = [delta async for delta in provider.stream(request(), token)]
        await provider._client.aclose()

    asyncio.run(scenario())


def test_invalid_provider_configuration_and_closed_lifecycle() -> None:
    with pytest.raises(ValueError, match="provider_endpoint_invalid"):
        OpenAICompatibleLLMProvider(base_url="file:///tmp", model="m", api_key="k")
    with pytest.raises(ValueError, match="model"):
        OpenAICompatibleLLMProvider(base_url="https://example.invalid", model=" ", api_key="k")
    with pytest.raises(ValueError, match="api_key"):
        OpenAICompatibleLLMProvider(base_url="https://example.invalid", model="m", api_key=" ")
    with pytest.raises(ValueError, match="default_temperature"):
        OpenAICompatibleLLMProvider(
            base_url="https://example.invalid",
            model="m",
            api_key="k",
            default_temperature=2.1,
        )
    with pytest.raises(ValueError, match="default_max_tokens"):
        OpenAICompatibleLLMProvider(
            base_url="https://example.invalid",
            model="m",
            api_key="k",
            default_max_tokens=0,
        )

    async def scenario() -> None:
        provider = OpenAICompatibleLLMProvider(
            base_url="https://provider.invalid", model="m", api_key="k"
        )
        await provider.close()
        await provider.close()
        with pytest.raises(RuntimeError, match="已关闭"):
            await provider.complete(request(), CancellationToken("turn"))

    asyncio.run(scenario())


def test_multimodal_payload_and_list_stream_content() -> None:
    seen: dict[str, object] = {}

    def handler(incoming: httpx.Request) -> httpx.Response:
        seen.update(json.loads(incoming.content))
        return httpx.Response(
            200,
            text="\n".join(
                [
                    "event: message",
                    'data: {"choices":[{"delta":null},{"delta":{"content":'
                    '[{"text":"图像"},7,{"ignored":true}]}}]}',
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                    "data: [DONE]",
                ]
            ),
        )

    multimodal = ChatRequest(
        messages=[
            ChatMessage(
                role=ChatRole.user,
                content=[
                    TextContent(text="看图"),
                    ImageURLContent(url="https://example.invalid/image.png", detail="high"),
                ],
            )
        ]
    )

    async def scenario() -> list[str]:
        provider = OpenAICompatibleLLMProvider(
            base_url="https://provider.invalid",
            endpoint="v1/chat/completions",
            model="test-model",
            api_key="fake-test-key",
            client=httpx.AsyncClient(
                base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
            ),
        )
        output = [delta async for delta in provider.stream(multimodal, CancellationToken("turn"))]
        await provider._client.aclose()
        return output

    assert asyncio.run(scenario()) == ["图像"]
    assert seen["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://example.invalid/image.png",
                        "detail": "high",
                    },
                },
            ],
        }
    ]


@pytest.mark.parametrize(
    ("operation", "exception", "expected"),
    [
        ("stream", httpx.ReadTimeout("slow provider"), LLMErrorCode.timeout),
        ("stream", httpx.ConnectError("offline"), LLMErrorCode.connection),
        ("complete", httpx.ReadTimeout("slow provider"), LLMErrorCode.timeout),
        ("complete", httpx.ConnectError("offline"), LLMErrorCode.connection),
    ],
)
def test_transport_errors_are_mapped(
    operation: str,
    exception: httpx.HTTPError,
    expected: LLMErrorCode,
) -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        raise exception

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler))
        with pytest.raises(LLMProviderError) as captured:
            if operation == "stream":
                _ = [delta async for delta in provider.stream(request(), CancellationToken("turn"))]
            else:
                await provider.complete(request(), CancellationToken("turn"))
        assert captured.value.code is expected
        assert captured.value.retryable
        await provider._client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "private remote response"}},
        {"choices": []},
        {"choices": [{}]},
    ],
)
def test_remote_and_completion_protocol_errors_are_rejected(body: dict[str, object]) -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler))
        with pytest.raises(LLMProviderError) as captured:
            await provider.complete(request(), CancellationToken("turn"))
        assert captured.value.code in {LLMErrorCode.rejected, LLMErrorCode.protocol}
        assert "private remote response" not in str(captured.value)
        await provider._client.aclose()

    asyncio.run(scenario())


def test_non_object_json_is_a_protocol_error() -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="[]")

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler))
        with pytest.raises(LLMProviderError, match="llm_protocol_error"):
            await provider.complete(request(), CancellationToken("turn"))
        await provider._client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mode", "events"),
    [
        (
            StreamCompletionMode.done_and_finish_reason,
            [
                'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                "data: [DONE]\n\n",
            ],
        ),
        (
            StreamCompletionMode.done,
            ['data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', "data: [DONE]\n\n"],
        ),
        (
            StreamCompletionMode.finish_reason,
            [
                'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            ],
        ),
        (
            StreamCompletionMode.eof,
            ['data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'],
        ),
    ],
)
def test_stream_completion_modes_make_done_finish_reason_and_legal_eof_explicit(
    mode: StreamCompletionMode, events: list[str]
) -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="".join(events))

    async def scenario() -> list[str]:
        provider = make_provider(httpx.MockTransport(handler), completion_mode=mode)
        try:
            return [delta async for delta in provider.stream(request(), CancellationToken("turn"))]
        finally:
            await provider.close()

    assert asyncio.run(scenario()) == ["ok"]


@pytest.mark.parametrize(
    ("mode", "finish_event"),
    [
        (StreamCompletionMode.finish_reason, ""),
        (
            StreamCompletionMode.finish_reason,
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        ),
        (StreamCompletionMode.eof, ""),
    ],
)
def test_stream_rejects_done_marker_that_bypasses_declared_completion_capability(
    mode: StreamCompletionMode,
    finish_event: str,
) -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                + finish_event
                + "data: [DONE]\n\n"
            ),
        )

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler), completion_mode=mode)
        emitted: list[str] = []
        try:
            with pytest.raises(LLMProviderError) as captured:
                async for delta in provider.stream(request(), CancellationToken(f"done-{mode}")):
                    emitted.append(delta)
            assert emitted == ["partial"]
            assert captured.value.code is LLMErrorCode.protocol
            assert "partial" not in str(captured.value)
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (
            [
                'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            ],
            LLMErrorCode.truncated,
        ),
        (
            ['data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', "data: [DONE]\n\n"],
            LLMErrorCode.protocol,
        ),
        (
            [
                'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
                "data: [DONE]\n\n",
            ],
            LLMErrorCode.truncated,
        ),
        (
            [
                'data: {"choices":[{"delta":{},"finish_reason":"synthetic_unknown"}]}\n\n',
                "data: [DONE]\n\n",
            ],
            LLMErrorCode.protocol,
        ),
    ],
)
def test_strict_stream_rejects_truncated_eof_missing_finish_and_bad_finish_reason(
    events: list[str], expected: LLMErrorCode
) -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="".join(events))

    async def scenario() -> None:
        provider = make_provider(
            httpx.MockTransport(handler),
            completion_mode=StreamCompletionMode.done_and_finish_reason,
        )
        try:
            with pytest.raises(LLMProviderError) as captured:
                _ = [delta async for delta in provider.stream(request(), CancellationToken("turn"))]
            assert captured.value.code is expected
            assert "partial" not in str(captured.value)
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_strict_stream_rejects_conflicting_finish_reasons() -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}\n\n'
            ),
        )

    async def scenario() -> None:
        provider = make_provider(
            httpx.MockTransport(handler),
            completion_mode=StreamCompletionMode.finish_reason,
        )
        try:
            with pytest.raises(LLMProviderError, match="llm_request_rejected"):
                _ = [
                    delta
                    async for delta in provider.stream(request(), CancellationToken("conflict"))
                ]
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["stream", "complete"])
def test_unclassified_http_transport_errors_are_content_free(operation: str) -> None:
    provider_body_sentinel = "remote-provider-body-secret"

    def handler(_incoming: httpx.Request) -> httpx.Response:
        raise httpx.HTTPError(provider_body_sentinel)

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler))
        try:
            with pytest.raises(LLMProviderError) as captured:
                if operation == "stream":
                    _ = [
                        delta
                        async for delta in provider.stream(
                            request(), CancellationToken("http-error")
                        )
                    ]
                else:
                    await provider.complete(request(), CancellationToken("http-error"))
            assert captured.value.code is LLMErrorCode.unavailable
            assert provider_body_sentinel not in str(captured.value)
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_closed_provider_rejects_new_work_and_close_is_idempotent() -> None:
    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(lambda _request: httpx.Response(200)))
        await provider.close()
        await provider.close()
        with pytest.raises(RuntimeError):
            await provider.complete(request(), CancellationToken("closed"))

    asyncio.run(scenario())


def test_duplicate_stream_delta_is_bounded_and_not_silently_retried() -> None:
    calls = 0

    def handler(_incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"same"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"same"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    async def scenario() -> list[str]:
        provider = make_provider(httpx.MockTransport(handler))
        try:
            return [delta async for delta in provider.stream(request(), CancellationToken("turn"))]
        finally:
            await provider.close()

    assert asyncio.run(scenario()) == ["same", "same"]
    assert calls == 1


def test_oversized_stream_event_is_rejected_by_the_existing_w07_byte_budget() -> None:
    provider_body_sentinel = "provider-body-must-not-escape"

    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"content":"'
                + provider_body_sentinel
                + '"}}]}\n\ndata: [DONE]\n\n'
            ),
        )

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler), max_stream_event_bytes=24)
        try:
            with pytest.raises(LLMProviderError) as captured:
                _ = [
                    delta
                    async for delta in provider.stream(request(), CancellationToken("bounded"))
                ]
            assert captured.value.code is LLMErrorCode.truncated
            assert provider_body_sentinel not in str(captured.value)
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_oversized_non_stream_body_is_rejected_before_json_buffering() -> None:
    provider_body_sentinel = "provider-non-stream-body-must-not-escape"
    stream = FragmentedByteStream(
        [
            b'{"choices":[{"message":{"content":"',
            provider_body_sentinel.encode() * 32,
            b'"},"finish_reason":"stop"}]}',
        ]
    )

    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler), max_stream_event_bytes=128)
        try:
            with pytest.raises(LLMProviderError) as captured:
                await provider.complete(request(), CancellationToken("bounded-complete"))
            assert captured.value.code is LLMErrorCode.truncated
            assert provider_body_sentinel not in str(captured.value)
            assert stream.closed
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_non_stream_length_finish_is_not_returned_as_success() -> None:
    def handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "partial-provider-body"}, "finish_reason": "length"}
                ]
            },
        )

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler))
        try:
            with pytest.raises(LLMProviderError) as captured:
                await provider.complete(request(), CancellationToken("turn"))
            assert captured.value.code is LLMErrorCode.truncated
            assert "partial-provider-body" not in str(captured.value)
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_https_redirect_is_never_followed_or_downgraded_to_http() -> None:
    calls: list[str] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(str(incoming.url))
        return httpx.Response(302, headers={"location": "http://provider.example/plaintext"})

    async def scenario() -> None:
        provider = make_provider(httpx.MockTransport(handler))
        try:
            with pytest.raises(LLMProviderError) as captured:
                _ = [
                    delta
                    async for delta in provider.stream(request(), CancellationToken("redirect"))
                ]
            assert captured.value.code is LLMErrorCode.protocol
        finally:
            await provider.close()

    asyncio.run(scenario())
    assert len(calls) == 1
    assert all(url.startswith("https://") for url in calls)
