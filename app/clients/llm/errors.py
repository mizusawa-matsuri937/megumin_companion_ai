"""Privacy-safe error taxonomy for remote LLM providers."""

from __future__ import annotations

from enum import StrEnum


class LLMErrorCode(StrEnum):
    authentication = "llm_authentication_failed"
    rate_limited = "llm_rate_limited"
    timeout = "llm_timeout"
    unavailable = "llm_unavailable"
    protocol = "llm_protocol_error"
    rejected = "llm_request_rejected"


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        code: LLMErrorCode,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
