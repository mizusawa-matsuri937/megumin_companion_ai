"""Strict privacy and parsing tests for LLM-backed memory analysis."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import cast

import pytest
from app.core.cancellation import CancellationToken
from app.memory.analyzer import (
    LLMMemoryCandidateAnalyzer,
    MemoryCandidateAnalyzer,
    NoopMemoryCandidateAnalyzer,
)
from app.memory.models import MemoryType
from app.schemas import (
    ChatCompletion,
    ChatRequest,
    ChatRole,
    PerceptionContext,
    ProactiveIntent,
    UserMessage,
)


class _FakeLLMProvider:
    def __init__(self, response: str = '{"claims":[]}', error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[ChatRequest] = []
        self.close_count = 0

    async def stream(
        self,
        request: ChatRequest,
        token: CancellationToken,
    ) -> AsyncIterator[str]:
        token.raise_if_cancelled()
        yield self.response

    async def complete(
        self,
        request: ChatRequest,
        token: CancellationToken,
    ) -> ChatCompletion:
        token.raise_if_cancelled()
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ChatCompletion(text=self.response, model="fake")

    async def close(self) -> None:
        self.close_count += 1


def _claim(
    *,
    key: str = "fact:coffee",
    content: str = "用户喜欢手冲咖啡",
    evidence: str = "我喜欢手冲咖啡",
) -> dict[str, object]:
    return {
        "memory_type": "fact",
        "canonical_key": key,
        "content": content,
        "evidence_quote": evidence,
        "importance_score": 0.8,
        "confidence_score": 0.95,
        "sensitivity_hint": "normal",
        "related_emotion": None,
    }


def _response(claims: list[dict[str, object]]) -> str:
    return json.dumps({"claims": claims}, ensure_ascii=False)


def test_protocol_noop_and_valid_json_mode_request() -> None:
    async def scenario() -> None:
        source = "我喜欢手冲咖啡，周末经常自己冲。"
        provider = _FakeLLMProvider(_response([_claim()]))
        analyzer: MemoryCandidateAnalyzer = LLMMemoryCandidateAnalyzer(provider)
        noop: MemoryCandidateAnalyzer = NoopMemoryCandidateAnalyzer()
        message = UserMessage(text=source)
        token = CancellationToken("turn_test")

        assert await noop.analyze(message, token) == []
        claims = await analyzer.analyze(message, token)

        assert len(claims) == 1
        assert claims[0].memory_type is MemoryType.fact
        assert claims[0].canonical_key == "fact:coffee"
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request.response_format == "json_object"
        assert request.temperature == 0.0
        assert request.max_tokens == 4_096
        assert [item.role for item in request.messages] == [ChatRole.system, ChatRole.user]
        assert request.messages[1].content == source
        assert message.user_id not in str(request.model_dump())
        await noop.close()
        await analyzer.close()
        assert provider.close_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "source",
    [
        "我的密码是 correct-horse-battery-staple",
        "请记住 API key: sk-abcdefghijklmnop",
        "验证码是 123456",
        "银行卡号 4111 1111 1111 1111",
    ],
)
def test_credentials_are_rejected_before_any_llm_call(source: str) -> None:
    async def scenario() -> None:
        provider = _FakeLLMProvider(_response([_claim()]))
        analyzer = LLMMemoryCandidateAnalyzer(provider)

        assert (
            await analyzer.analyze(UserMessage(text=source), CancellationToken("turn_test")) == []
        )
        assert provider.requests == []

    asyncio.run(scenario())


def test_screen_and_proactive_values_fail_closed_at_runtime_boundary() -> None:
    async def scenario() -> None:
        provider = _FakeLLMProvider(_response([_claim()]))
        analyzer = LLMMemoryCandidateAnalyzer(provider)
        token = CancellationToken("turn_test")
        screen = PerceptionContext(summary="我喜欢手冲咖啡")
        proactive = ProactiveIntent(
            trigger_type="idle",
            instruction="我喜欢手冲咖啡",
            score=0.8,
            reason="test",
        )

        assert await analyzer.analyze(cast(UserMessage, screen), token) == []
        assert await analyzer.analyze(cast(UserMessage, proactive), token) == []
        assert provider.requests == []

    asyncio.run(scenario())


def _malformed_responses() -> list[str]:
    valid = _claim()
    extra = {**valid, "unexpected": True}
    coerced = {**valid, "importance_score": "0.8"}
    hallucinated = {**valid, "evidence_quote": "用户没有说过这句"}
    duplicate_claim = {**valid}
    too_many = [{**valid, "canonical_key": f"fact:item_{index}"} for index in range(6)]
    return [
        "not json",
        '```json\n{"claims":[]}\n```',
        "[]",
        '{"claims":[],"extra":true}',
        '{"claims":{}}',
        '{"claims":[1]}',
        '{"claims":[],"claims":[]}',
        '{"claims":[{"importance_score":NaN}]}',
        _response([extra]),
        _response([coerced]),
        _response([hallucinated]),
        _response([valid, duplicate_claim]),
        _response(too_many),
        "x" * 50_001,
    ]


@pytest.mark.parametrize("response", _malformed_responses())
def test_malformed_or_ungrounded_response_is_all_or_nothing(response: str) -> None:
    async def scenario() -> None:
        provider = _FakeLLMProvider(response)
        analyzer = LLMMemoryCandidateAnalyzer(provider)
        message = UserMessage(text="我喜欢手冲咖啡，周末经常自己冲。")

        assert await analyzer.analyze(message, CancellationToken("turn_test")) == []

    asyncio.run(scenario())


def test_exactly_five_distinct_grounded_claims_are_accepted() -> None:
    async def scenario() -> None:
        claims = [_claim(key=f"fact:coffee_{index}") for index in range(5)]
        provider = _FakeLLMProvider(_response(claims))
        analyzer = LLMMemoryCandidateAnalyzer(provider)

        result = await analyzer.analyze(
            UserMessage(text="我喜欢手冲咖啡。"), CancellationToken("turn_test")
        )

        assert len(result) == 5
        assert len({claim.canonical_key for claim in result}) == 5

    asyncio.run(scenario())


def test_hallucinated_credential_claim_is_rejected() -> None:
    async def scenario() -> None:
        malicious = _claim(content="password: leaked-secret")
        provider = _FakeLLMProvider(_response([malicious]))
        analyzer = LLMMemoryCandidateAnalyzer(provider)

        result = await analyzer.analyze(
            UserMessage(text="我喜欢手冲咖啡。"), CancellationToken("turn_test")
        )

        assert result == []

    asyncio.run(scenario())


def test_remote_error_is_opaque_and_does_not_log_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        source = "这是只应存在于显式消息里的私密正文"
        provider = _FakeLLMProvider(error=RuntimeError(f"remote echoed: {source}"))
        analyzer = LLMMemoryCandidateAnalyzer(provider)

        assert (
            await analyzer.analyze(UserMessage(text=source), CancellationToken("turn_test")) == []
        )
        assert source not in caplog.text

    asyncio.run(scenario())


def test_cancellation_propagates_without_calling_provider() -> None:
    async def scenario() -> None:
        provider = _FakeLLMProvider()
        analyzer = LLMMemoryCandidateAnalyzer(provider)
        token = CancellationToken("turn_test")
        token.cancel()

        with pytest.raises(asyncio.CancelledError):
            await analyzer.analyze(UserMessage(text="我喜欢手冲咖啡"), token)
        assert provider.requests == []

    asyncio.run(scenario())


def test_owned_provider_close_is_idempotent_and_closed_analyzer_is_safe() -> None:
    async def scenario() -> None:
        provider = _FakeLLMProvider()
        analyzer = LLMMemoryCandidateAnalyzer(provider, owns_provider=True)
        await analyzer.close()
        await analyzer.close()

        assert provider.close_count == 1
        with pytest.raises(RuntimeError, match="analyzer is closed") as error:
            await analyzer.analyze(
                UserMessage(text="正文不应进入异常"), CancellationToken("turn_test")
            )
        assert "正文不应进入异常" not in str(error.value)

    asyncio.run(scenario())
