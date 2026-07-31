"""Fixed, text-only DeepSeek V4 Flash provider profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.clients.llm.errors import LLMErrorCode, LLMProviderError
from app.clients.llm.openai_compatible import (
    OpenAICompatibleLLMProvider,
    StreamCompletionMode,
    _raise_remote_error,
)
from app.limits import LimitsConfig
from app.schemas import ChatRequest

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"


class DeepSeekFlashLLMProvider(OpenAICompatibleLLMProvider):
    """A constrained DeepSeek profile which cannot drift into multimodal use."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 45.0,
        default_temperature: float | None = None,
        default_max_tokens: int | None = None,
        max_stream_event_bytes: int = LimitsConfig().llm_output_bytes,
        proxy_url: str | None = None,
        ca_bundle_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=DEEPSEEK_BASE_URL,
            endpoint=DEEPSEEK_CHAT_COMPLETIONS_ENDPOINT,
            model=DEEPSEEK_FLASH_MODEL,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            default_temperature=default_temperature,
            default_max_tokens=default_max_tokens,
            max_stream_event_bytes=max_stream_event_bytes,
            stream_completion_mode=StreamCompletionMode.done_and_finish_reason,
            proxy_url=proxy_url,
            ca_bundle_path=ca_bundle_path,
            transport=transport,
            client=client,
            finish_reason_classifier=_classify_deepseek_finish_reason,
            remote_error_classifier=_raise_deepseek_remote_error,
            status_error_classifier=_classify_deepseek_status_error,
            reject_tool_calls=True,
        )

    def _payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        _reject_non_string_content(request)
        payload = super()._payload(request, stream=stream)
        payload["thinking"] = {"type": "disabled"}
        return payload


def _reject_non_string_content(request: ChatRequest) -> None:
    """Accept DeepSeek's text-only string message shape and nothing else.

    The fixed profile must neither serialize image content nor silently send
    OpenAI multipart text content to a provider whose documented message shape
    is a string.  Prompt construction already produces strings, so callers
    that need a different content shape fail locally before HTTP.
    """

    for message in request.messages:
        if not isinstance(message.content, str):
            raise LLMProviderError(LLMErrorCode.rejected, retryable=False)


def _classify_deepseek_finish_reason(reason: str) -> None:
    if reason == "insufficient_system_resource":
        raise LLMProviderError(LLMErrorCode.unavailable, retryable=True)
    if reason in {"tool_calls", "function_call"}:
        raise LLMProviderError(LLMErrorCode.rejected, retryable=False)
    if reason == "stop":
        return
    if reason == "length":
        raise LLMProviderError(LLMErrorCode.truncated, retryable=False)
    if reason == "content_filter":
        raise LLMProviderError(LLMErrorCode.rejected, retryable=False)
    raise LLMProviderError(LLMErrorCode.protocol, retryable=False)


def _raise_deepseek_remote_error(body: dict[str, Any]) -> None:
    """Keep DeepSeek resource exhaustion retryable in both response forms."""

    error = body.get("error")
    if isinstance(error, dict) and any(
        error.get(field) == "insufficient_system_resource" for field in ("code", "type")
    ):
        raise LLMProviderError(LLMErrorCode.unavailable, retryable=True)
    _raise_remote_error(body)


def _classify_deepseek_status_error(
    _status_code: int,
    body: dict[str, Any],
) -> LLMProviderError | None:
    """Recognize resource exhaustion even when DeepSeek uses HTTP 4xx."""

    error = body.get("error")
    if isinstance(error, dict) and any(
        error.get(field) == "insufficient_system_resource" for field in ("code", "type")
    ):
        return LLMProviderError(LLMErrorCode.unavailable, retryable=True)
    return None
