"""DeepSeek Flash's fixed profile and text-only boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from app.clients.llm import (
    DeepSeekFlashLLMProvider,
    LLMErrorCode,
    LLMProviderError,
)
from app.core import CancellationToken
from app.schemas import ChatMessage, ChatRequest, ChatRole, ImageURLContent, TextContent


def _request() -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role=ChatRole.user, content="合成测试请求")],
        temperature=0.25,
        max_tokens=123,
        response_format="json_object",
    )


def test_flash_stream_uses_fixed_endpoint_model_and_disabled_thinking() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> list[str]:
        provider = DeepSeekFlashLLMProvider(
            api_key="deepseek-test-key-not-real",
            transport=httpx.MockTransport(handler),
        )
        try:
            return [part async for part in provider.stream(_request(), CancellationToken("turn"))]
        finally:
            await provider.close()

    assert asyncio.run(scenario()) == ["好"]
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["authorization"] == "Bearer deepseek-test-key-not-real"
    assert seen["payload"] == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "合成测试请求"}],
        "stream": True,
        "temperature": 0.25,
        "max_tokens": 123,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }


@pytest.mark.parametrize(
    "content",
    [
        [
            TextContent(text="不要上传此图片"),
            ImageURLContent(url="https://example.invalid/private.png"),
        ],
        [TextContent(text="也不要发送 OpenAI multipart 文本")],
    ],
)
def test_flash_rejects_non_string_content_before_any_http_request(
    content: list[TextContent | ImageURLContent],
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    request = ChatRequest(
        messages=[
            ChatMessage(
                role=ChatRole.user,
                content=content,
            )
        ]
    )

    async def scenario() -> LLMProviderError:
        provider = DeepSeekFlashLLMProvider(
            api_key="deepseek-test-key-not-real",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(LLMProviderError) as captured:
                await provider.complete(request, CancellationToken("turn"))
            return captured.value
        finally:
            await provider.close()

    error = asyncio.run(scenario())
    assert error.code is LLMErrorCode.rejected
    assert not error.retryable
    assert calls == 0


def test_flash_rejects_streamed_tool_calls_before_exposing_any_delta() -> None:
    async def scenario() -> LLMProviderError:
        provider = DeepSeekFlashLLMProvider(
            api_key="deepseek-test-key-not-real",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    text=('data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1"}]}}]}\n\n'),
                    headers={"content-type": "text/event-stream"},
                )
            ),
        )
        try:
            with pytest.raises(LLMProviderError) as captured:
                _ = [part async for part in provider.stream(_request(), CancellationToken("turn"))]
            return captured.value
        finally:
            await provider.close()

    error = asyncio.run(scenario())
    assert error.code is LLMErrorCode.rejected
    assert not error.retryable


@pytest.mark.parametrize(
    "choice",
    [
        {"message": {"tool_calls": [{"id": "call_message"}]}, "finish_reason": "stop"},
        {"tool_calls": [{"id": "call_choice"}], "finish_reason": "stop"},
    ],
)
def test_flash_rejects_all_stream_choice_tool_call_shapes(choice: dict[str, object]) -> None:
    async def scenario() -> LLMProviderError:
        provider = DeepSeekFlashLLMProvider(
            api_key="deepseek-test-key-not-real",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    text=f"data: {json.dumps({'choices': [choice]})}\n\n",
                    headers={"content-type": "text/event-stream"},
                )
            ),
        )
        try:
            with pytest.raises(LLMProviderError) as captured:
                _ = [part async for part in provider.stream(_request(), CancellationToken("turn"))]
            return captured.value
        finally:
            await provider.close()

    error = asyncio.run(scenario())
    assert error.code is LLMErrorCode.rejected
    assert not error.retryable


def test_flash_stream_maps_resource_error_as_retryable_unavailable() -> None:
    async def scenario() -> LLMProviderError:
        provider = DeepSeekFlashLLMProvider(
            api_key="deepseek-test-key-not-real",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    text=('data: {"error":{"code":"insufficient_system_resource"}}\n\n'),
                    headers={"content-type": "text/event-stream"},
                )
            ),
        )
        try:
            with pytest.raises(LLMProviderError) as captured:
                _ = [part async for part in provider.stream(_request(), CancellationToken("turn"))]
            return captured.value
        finally:
            await provider.close()

    error = asyncio.run(scenario())
    assert error.code is LLMErrorCode.unavailable
    assert error.retryable


def test_flash_stream_maps_http_resource_error_as_retryable_unavailable() -> None:
    async def scenario() -> LLMProviderError:
        provider = DeepSeekFlashLLMProvider(
            api_key="deepseek-test-key-not-real",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    400,
                    json={"error": {"type": "insufficient_system_resource"}},
                )
            ),
        )
        try:
            with pytest.raises(LLMProviderError) as captured:
                _ = [part async for part in provider.stream(_request(), CancellationToken("turn"))]
            return captured.value
        finally:
            await provider.close()

    error = asyncio.run(scenario())
    assert error.code is LLMErrorCode.unavailable
    assert error.retryable


@pytest.mark.parametrize(
    ("handler", "expected", "retryable"),
    [
        (
            lambda _request: httpx.Response(
                400,
                json={"error": {"code": "insufficient_system_resource"}},
            ),
            LLMErrorCode.unavailable,
            True,
        ),
        (
            lambda _request: httpx.Response(401, json={"error": {"message": "bad key"}}),
            LLMErrorCode.authentication,
            False,
        ),
        (
            lambda _request: httpx.Response(429, json={"error": {"message": "limited"}}),
            LLMErrorCode.rate_limited,
            True,
        ),
        (
            lambda request: _raise_timeout(request),
            LLMErrorCode.timeout,
            True,
        ),
    ],
)
def test_flash_preserves_safe_http_and_timeout_mapping(
    handler: Callable[[httpx.Request], httpx.Response],
    expected: LLMErrorCode,
    retryable: bool,
) -> None:
    async def scenario() -> LLMProviderError:
        provider = DeepSeekFlashLLMProvider(
            api_key="deepseek-test-key-not-real",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(LLMProviderError) as captured:
                await provider.complete(_request(), CancellationToken("turn"))
            return captured.value
        finally:
            await provider.close()

    error = asyncio.run(scenario())
    assert error.code is expected
    assert error.retryable is retryable


def _raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("synthetic timeout", request=request)


@pytest.mark.parametrize(
    ("body", "expected", "retryable"),
    [
        (
            {
                "choices": [
                    {
                        "message": {"content": ""},
                        "finish_reason": "insufficient_system_resource",
                    }
                ]
            },
            LLMErrorCode.unavailable,
            True,
        ),
        (
            {"error": {"code": "insufficient_system_resource"}},
            LLMErrorCode.unavailable,
            True,
        ),
        (
            {
                "choices": [
                    {
                        "message": {"tool_calls": [{"id": "call_1"}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            LLMErrorCode.rejected,
            False,
        ),
        (
            {
                "choices": [
                    {
                        "message": {"content": "safe-first-choice"},
                        "finish_reason": "stop",
                    },
                    {
                        "message": {"tool_calls": [{"id": "call_2"}]},
                        "finish_reason": "tool_calls",
                    },
                ]
            },
            LLMErrorCode.rejected,
            False,
        ),
    ],
)
def test_flash_maps_resource_and_tool_responses_without_exposing_them(
    body: dict[str, object], expected: LLMErrorCode, retryable: bool
) -> None:
    async def scenario() -> LLMProviderError:
        provider = DeepSeekFlashLLMProvider(
            api_key="deepseek-test-key-not-real",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
        )
        try:
            with pytest.raises(LLMProviderError) as captured:
                await provider.complete(_request(), CancellationToken("turn"))
            return captured.value
        finally:
            await provider.close()

    error = asyncio.run(scenario())
    assert error.code is expected
    assert error.retryable is retryable
