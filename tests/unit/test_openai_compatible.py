"""OpenAI-compatible payload, streaming, errors, and lifecycle tests."""

import asyncio
import json

import httpx
import pytest
from app.clients.llm import LLMErrorCode, LLMProviderError, OpenAICompatibleLLMProvider
from app.core import CancellationToken
from app.schemas import (
    ChatMessage,
    ChatRequest,
    ChatRole,
    ImageURLContent,
    TextContent,
)


def make_provider(handler: httpx.MockTransport) -> OpenAICompatibleLLMProvider:
    client = httpx.AsyncClient(base_url="https://provider.invalid", transport=handler)
    return OpenAICompatibleLLMProvider(
        base_url="https://provider.invalid",
        model="test-model",
        api_key="fake-test-key",
        client=client,
    )


def request() -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role=ChatRole.user, content="你好")],
        temperature=0.2,
        max_tokens=50,
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
    with pytest.raises(ValueError, match="HTTP"):
        OpenAICompatibleLLMProvider(base_url="file:///tmp", model="m", api_key="k")
    with pytest.raises(ValueError, match="model"):
        OpenAICompatibleLLMProvider(base_url="https://example.invalid", model=" ", api_key="k")
    with pytest.raises(ValueError, match="api_key"):
        OpenAICompatibleLLMProvider(base_url="https://example.invalid", model="m", api_key=" ")

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
                    'data: {"choices":[null,{"delta":null},{"delta":{"content":'
                    '[{"text":"图像"},7,{"ignored":true}]}}]}',
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
        ("stream", httpx.ConnectError("offline"), LLMErrorCode.unavailable),
        ("complete", httpx.ReadTimeout("slow provider"), LLMErrorCode.timeout),
        ("complete", httpx.ConnectError("offline"), LLMErrorCode.unavailable),
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
