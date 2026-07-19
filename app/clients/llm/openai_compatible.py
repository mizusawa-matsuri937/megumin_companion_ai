"""OpenAI-compatible Chat Completions client with strict error handling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import httpx

from app.clients.llm.errors import LLMErrorCode, LLMProviderError
from app.core.cancellation import CancellationToken
from app.limits import LimitsConfig
from app.provider_transport import (
    EndpointKind,
    build_ssl_context,
    validate_endpoint,
    validate_proxy_url,
)
from app.schemas import (
    ChatCompletion,
    ChatMessage,
    ChatRequest,
    ImageURLContent,
    TextContent,
)

_ResultT = TypeVar("_ResultT")


class StreamCompletionMode(StrEnum):
    """Provider capability for declaring one streamed text response complete."""

    done_and_finish_reason = "done_and_finish_reason"
    done = "done"
    finish_reason = "finish_reason"
    eof = "eof"


class OpenAICompatibleLLMProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        endpoint: str = "/v1/chat/completions",
        timeout_seconds: float = 45.0,
        default_temperature: float | None = None,
        default_max_tokens: int | None = None,
        max_stream_event_bytes: int = LimitsConfig().llm_output_bytes,
        stream_completion_mode: StreamCompletionMode
        | str = StreamCompletionMode.done_and_finish_reason,
        proxy_url: str | None = None,
        ca_bundle_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        validate_endpoint(base_url, kind=EndpointKind.http)
        if not model.strip():
            raise ValueError("LLM model 不能为空")
        if not api_key.strip():
            raise ValueError("LLM api_key 不能为空")
        if default_temperature is not None and not 0.0 <= default_temperature <= 2.0:
            raise ValueError("LLM default_temperature 必须介于 0 和 2 之间")
        if default_max_tokens is not None and not 1 <= default_max_tokens <= 100_000:
            raise ValueError("LLM default_max_tokens 必须介于 1 和 100000 之间")
        if not 1 <= max_stream_event_bytes <= LimitsConfig().llm_output_bytes:
            raise ValueError("LLM stream event byte limit exceeds W07 output budget")
        if client is not None and transport is not None:
            raise ValueError("LLM test transport must have one owner")
        if proxy_url is not None:
            validate_proxy_url(proxy_url)
        self._model = model
        self._endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._max_stream_event_bytes = max_stream_event_bytes
        self._stream_completion_mode = StreamCompletionMode(stream_completion_mode)
        if client is not None:
            transport = client._transport
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds),
            verify=build_ssl_context(ca_bundle_path),
            proxy=proxy_url,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._closed = False

    async def stream(self, request: ChatRequest, token: CancellationToken) -> AsyncIterator[str]:
        self._ensure_open()
        token.raise_if_cancelled()
        payload = self._payload(request, stream=True)
        response: httpx.Response | None = None
        try:
            outgoing = self._client.build_request("POST", self._endpoint, json=payload)
            response = await _await_with_token(
                self._client.send(outgoing, stream=True),
                token,
            )
            try:
                token.raise_if_cancelled()
                self._raise_for_status(response)
                lines = _bounded_lines(
                    response,
                    token,
                    max_line_bytes=self._max_stream_event_bytes,
                )
                saw_event = False
                finish_reason: str | None = None
                while True:
                    try:
                        line = await _await_with_token(anext(lines), token)
                    except StopAsyncIteration:
                        _validate_stream_eof(
                            self._stream_completion_mode,
                            saw_event=saw_event,
                            finish_reason=finish_reason,
                        )
                        return
                    token.raise_if_cancelled()
                    data = _sse_data(line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        _validate_done_marker(self._stream_completion_mode, finish_reason)
                        return
                    body = _parse_json(data)
                    _raise_remote_error(body)
                    deltas, event_finish_reason = _extract_stream_event(body)
                    saw_event = True
                    if event_finish_reason is not None:
                        _classify_finish_reason(event_finish_reason)
                        if finish_reason is not None and finish_reason != event_finish_reason:
                            raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
                        finish_reason = event_finish_reason
                    for delta in deltas:
                        if delta:
                            yield delta
            finally:
                await response.aclose()
        except LLMProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMProviderError(LLMErrorCode.timeout, retryable=True) from exc
        except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise LLMProviderError(LLMErrorCode.connection, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(LLMErrorCode.unavailable, retryable=True) from exc

    async def complete(self, request: ChatRequest, token: CancellationToken) -> ChatCompletion:
        self._ensure_open()
        token.raise_if_cancelled()
        response: httpx.Response | None = None
        try:
            outgoing = self._client.build_request(
                "POST",
                self._endpoint,
                json=self._payload(request, stream=False),
            )
            response = await _await_with_token(
                self._client.send(outgoing, stream=True),
                token,
            )
            try:
                token.raise_if_cancelled()
                self._raise_for_status(response)
                payload = await _read_bounded_body(
                    response,
                    token,
                    max_body_bytes=self._max_stream_event_bytes,
                )
                body = _parse_json(payload)
                _raise_remote_error(body)
                return _extract_completion(body)
            finally:
                await response.aclose()
        except LLMProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMProviderError(LLMErrorCode.timeout, retryable=True) from exc
        except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise LLMProviderError(LLMErrorCode.connection, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(LLMErrorCode.unavailable, retryable=True) from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
        temperature = (
            request.temperature if request.temperature is not None else self._default_temperature
        )
        max_tokens = (
            request.max_tokens if request.max_tokens is not None else self._default_max_tokens
        )
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if request.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 300 <= status < 400:
            raise LLMProviderError(LLMErrorCode.protocol, retryable=False, status_code=status)
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


async def _await_with_token(
    operation: Awaitable[_ResultT],
    token: CancellationToken,
) -> _ResultT:
    """Cancel one blocked HTTP operation as soon as its turn token is revoked."""

    token.raise_if_cancelled()
    operation_task = asyncio.ensure_future(operation)
    cancellation_task = asyncio.create_task(token.wait())
    try:
        done, _pending = await asyncio.wait(
            (operation_task, cancellation_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation_task in done:
            return operation_task.result()
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        token.raise_if_cancelled()
        raise asyncio.CancelledError
    except BaseException:
        if not operation_task.done():
            operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise
    finally:
        cancellation_task.cancel()
        await asyncio.gather(cancellation_task, return_exceptions=True)


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


async def _bounded_lines(
    response: httpx.Response,
    token: CancellationToken,
    *,
    max_line_bytes: int,
) -> AsyncIterator[str]:
    """Decode SSE lines incrementally without buffering an unbounded provider body."""

    buffer = bytearray()
    stream = response.aiter_bytes(chunk_size=min(max_line_bytes, 64 * 1024))
    while True:
        try:
            chunk = await _await_with_token(anext(stream), token)
        except StopAsyncIteration:
            break
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw = bytes(buffer[:newline]).rstrip(b"\r")
            del buffer[: newline + 1]
            if len(raw) > max_line_bytes:
                raise LLMProviderError(LLMErrorCode.truncated, retryable=False)
            try:
                yield raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LLMProviderError(LLMErrorCode.protocol, retryable=False) from exc
        if len(buffer) > max_line_bytes:
            raise LLMProviderError(LLMErrorCode.truncated, retryable=False)
    if buffer:
        if len(buffer) > max_line_bytes:
            raise LLMProviderError(LLMErrorCode.truncated, retryable=False)
        try:
            yield bytes(buffer).rstrip(b"\r").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LLMProviderError(LLMErrorCode.protocol, retryable=False) from exc


async def _read_bounded_body(
    response: httpx.Response,
    token: CancellationToken,
    *,
    max_body_bytes: int,
) -> str:
    """Read one non-stream response without exceeding the shared W07 byte budget."""

    chunks: list[bytes] = []
    byte_count = 0
    stream = response.aiter_bytes(chunk_size=min(max_body_bytes, 64 * 1024))
    while True:
        try:
            chunk = await _await_with_token(anext(stream), token)
        except StopAsyncIteration:
            break
        byte_count += len(chunk)
        if byte_count > max_body_bytes:
            raise LLMProviderError(LLMErrorCode.truncated, retryable=False)
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False) from exc


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


def _extract_stream_event(body: dict[str, Any]) -> tuple[list[str], str | None]:
    choices = body.get("choices")
    if not isinstance(choices, list):
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    output: list[str] = []
    finish_reasons: set[str] = set()
    for choice in choices:
        if not isinstance(choice, dict):
            raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
        raw_finish_reason = choice.get("finish_reason")
        if raw_finish_reason is not None:
            if not isinstance(raw_finish_reason, str):
                raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
            finish_reasons.add(raw_finish_reason)
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
    if len(finish_reasons) > 1:
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    return output, next(iter(finish_reasons), None)


def _classify_finish_reason(reason: str) -> None:
    if reason == "stop":
        return
    if reason == "length":
        raise LLMProviderError(LLMErrorCode.truncated, retryable=False)
    if reason == "content_filter":
        raise LLMProviderError(LLMErrorCode.rejected, retryable=False)
    raise LLMProviderError(LLMErrorCode.protocol, retryable=False)


def _validate_done_marker(mode: StreamCompletionMode, finish_reason: str | None) -> None:
    if mode in {StreamCompletionMode.finish_reason, StreamCompletionMode.eof}:
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    if mode is StreamCompletionMode.done_and_finish_reason and finish_reason is None:
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    if finish_reason is not None:
        _classify_finish_reason(finish_reason)


def _validate_stream_eof(
    mode: StreamCompletionMode,
    *,
    saw_event: bool,
    finish_reason: str | None,
) -> None:
    if not saw_event:
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    if mode is StreamCompletionMode.eof:
        return
    if mode is StreamCompletionMode.finish_reason and finish_reason is not None:
        _classify_finish_reason(finish_reason)
        return
    raise LLMProviderError(LLMErrorCode.truncated, retryable=False)


def _extract_completion(body: dict[str, Any]) -> ChatCompletion:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str):
        raise LLMProviderError(LLMErrorCode.protocol, retryable=False)
    _classify_finish_reason(finish_reason)
    usage_raw = body.get("usage")
    usage = (
        {key: value for key, value in usage_raw.items() if isinstance(value, int)}
        if isinstance(usage_raw, dict)
        else {}
    )
    return ChatCompletion(
        text=message["content"],
        finish_reason=finish_reason,
        model=body.get("model") if isinstance(body.get("model"), str) else None,
        usage=usage,
    )
