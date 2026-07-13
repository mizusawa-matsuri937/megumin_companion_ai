"""OpenAI-compatible Chat Completions client with strict error handling."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.clients.llm.errors import LLMErrorCode, LLMProviderError
from app.core.cancellation import CancellationToken
from app.schemas import (
    ChatCompletion,
    ChatMessage,
    ChatRequest,
    ImageURLContent,
    TextContent,
)


class OpenAICompatibleLLMProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        endpoint: str = "/v1/chat/completions",
        timeout_seconds: float = 45.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("LLM base_url 必须使用 HTTP(S)")
        if not model.strip():
            raise ValueError("LLM model 不能为空")
        if not api_key.strip():
            raise ValueError("LLM api_key 不能为空")
        self._model = model
        self._endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds),
        )
        self._closed = False

    async def stream(self, request: ChatRequest, token: CancellationToken) -> AsyncIterator[str]:
        self._ensure_open()
        payload = self._payload(request, stream=True)
        try:
            async with self._client.stream("POST", self._endpoint, json=payload) as response:
                self._raise_for_status(response)
                async for line in response.aiter_lines():
                    token.raise_if_cancelled()
                    data = _sse_data(line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        return
                    body = _parse_json(data)
                    _raise_remote_error(body)
                    for delta in _extract_stream_text(body):
                        if delta:
                            yield delta
        except LLMProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMProviderError(LLMErrorCode.timeout, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(LLMErrorCode.unavailable, retryable=True) from exc

    async def complete(self, request: ChatRequest, token: CancellationToken) -> ChatCompletion:
        self._ensure_open()
        token.raise_if_cancelled()
        try:
            response = await self._client.post(
                self._endpoint,
                json=self._payload(request, stream=False),
            )
            token.raise_if_cancelled()
            self._raise_for_status(response)
            body = _parse_json(response.text)
            _raise_remote_error(body)
            return _extract_completion(body)
        except LLMProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMProviderError(LLMErrorCode.timeout, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(LLMErrorCode.unavailable, retryable=True) from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LLM provider 已关闭")

    def _payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_serialize_message(message) for message in request.messages],
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status in {401, 403}:
            code, retryable = LLMErrorCode.authentication, False
        elif status == 429:
            code, retryable = LLMErrorCode.rate_limited, True
        elif status >= 500:
            code, retryable = LLMErrorCode.unavailable, True
        else:
            code, retryable = LLMErrorCode.rejected, False
        raise LLMProviderError(code, retryable=retryable, status_code=status)


def _serialize_message(message: ChatMessage) -> dict[str, Any]:
    if isinstance(message.content, str):
        content: str | list[dict[str, Any]] = message.content
    else:
        content = []
        for part in message.content:
            if isinstance(part, TextContent):
                content.append({"type": "text", "text": part.text})
            elif isinstance(part, ImageURLContent):
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": part.url, "detail": part.detail},
                    }
                )
    return {"role": message.role.value, "content": content}


def _sse_data(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(":"):
        return None
    if not stripped.startswith("data:"):
        return None
    return stripped[5:].lstrip()


def _parse_json(data: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False) from exc
    if not isinstance(value, dict):
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    return value


def _raise_remote_error(body: dict[str, Any]) -> None:
    if "error" in body:
        raise LLMProviderError(LLMErrorCode.rejected, retryable=False)


def _extract_stream_text(body: dict[str, Any]) -> list[str]:
    choices = body.get("choices")
    if not isinstance(choices, list):
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    output: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            output.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    output.append(part["text"])
    return output


def _extract_completion(body: dict[str, Any]) -> ChatCompletion:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    usage_raw = body.get("usage")
    usage = (
        {key: value for key, value in usage_raw.items() if isinstance(value, int)}
        if isinstance(usage_raw, dict)
        else {}
    )
    return ChatCompletion(
        text=message["content"],
        finish_reason=choice.get("finish_reason")
        if isinstance(choice.get("finish_reason"), str)
        else None,
        model=body.get("model") if isinstance(body.get("model"), str) else None,
        usage=usage,
    )
