"""W11 liveness, readiness, and capability aggregation contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from app.health import (
    CapabilityCheck,
    CapabilityState,
    HealthAggregator,
)


@dataclass
class _Provider:
    name: str
    required_for_readiness: bool
    result: Any
    calls: int = 0

    async def check_health(self) -> Any:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return await self.result()
        return self.result


class _ExplosiveHealthError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("W11_HEALTH_REPR_SENTINEL")

    def __repr__(self) -> str:
        raise RuntimeError("W11_HEALTH_REPR_SENTINEL")


def test_liveness_never_waits_for_or_calls_capability_providers() -> None:
    async def never() -> CapabilityCheck:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    provider = _Provider("worker", True, never)
    aggregate = HealthAggregator(
        service="megumin-companion-ai",
        version="test-version",
        providers=(provider,),
        timeout_seconds=0.01,
    )

    payload = asyncio.run(aggregate.liveness()).model_dump(mode="json", exclude_none=True)

    assert payload == {
        "schema_version": 1,
        "status": "live",
        "service": "megumin-companion-ai",
        "version": "test-version",
    }
    assert provider.calls == 0


def test_readiness_uses_only_required_providers_but_capabilities_report_all() -> None:
    core = _Provider("core", True, CapabilityCheck(status=CapabilityState.ready))
    optional = _Provider(
        "vts",
        False,
        CapabilityCheck(status=CapabilityState.degraded, error_code="vts_disconnected"),
    )
    aggregate = HealthAggregator(
        service="megumin-companion-ai",
        version="test-version",
        providers=(core, optional),
    )

    readiness = asyncio.run(aggregate.readiness()).model_dump(mode="json", exclude_none=True)
    capabilities = asyncio.run(aggregate.capabilities()).model_dump(mode="json", exclude_none=True)

    assert readiness["status"] == "ready"
    assert readiness["checks"] == [{"name": "core", "status": "ready"}]
    assert capabilities["capabilities"] == [
        {"name": "core", "status": "ready"},
        {"name": "vts", "status": "degraded", "error_code": "vts_disconnected"},
    ]


def test_required_failure_makes_readiness_not_ready_with_only_an_error_code() -> None:
    provider = _Provider(
        "database",
        True,
        CapabilityCheck(status=CapabilityState.unavailable, error_code="db_busy"),
    )
    aggregate = HealthAggregator(
        service="megumin-companion-ai",
        version="test-version",
        providers=(provider,),
    )

    payload = asyncio.run(aggregate.readiness()).model_dump(mode="json", exclude_none=True)

    assert payload == {
        "schema_version": 1,
        "status": "not_ready",
        "service": "megumin-companion-ai",
        "version": "test-version",
        "checks": [{"name": "database", "status": "unavailable", "error_code": "db_busy"}],
    }


def test_provider_exception_timeout_and_malicious_fields_fail_closed() -> None:
    async def never() -> CapabilityCheck:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    providers = (
        _Provider("exceptional", True, _ExplosiveHealthError()),
        _Provider("hung", True, never),
        _Provider(
            "malicious",
            True,
            {
                "status": "ready",
                "path": "C:\\Users\\private-user\\secret.txt",
                "message": "W11_HEALTH_BODY_SENTINEL",
            },
        ),
    )
    aggregate = HealthAggregator(
        service="megumin-companion-ai",
        version="test-version",
        providers=providers,
        timeout_seconds=0.01,
    )

    payload = asyncio.run(aggregate.readiness()).model_dump(mode="json", exclude_none=True)
    serialized = str(payload)

    assert payload["status"] == "not_ready"
    assert [item["error_code"] for item in payload["checks"]] == [
        "health_provider_failed",
        "health_provider_timeout",
        "health_provider_invalid",
    ]
    assert "private-user" not in serialized
    assert "W11_HEALTH_BODY_SENTINEL" not in serialized
    assert "W11_HEALTH_REPR_SENTINEL" not in serialized


def test_health_models_reject_unknown_fields_and_unsafe_error_codes() -> None:
    try:
        CapabilityCheck.model_validate({"status": "ready", "path": "C:\\private"})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown health fields must be rejected")

    try:
        CapabilityCheck(status=CapabilityState.unavailable, error_code="secret at C:\\private")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe error codes must be rejected")


def test_registered_provider_identity_and_readiness_role_cannot_mutate() -> None:
    provider = _Provider("core", True, CapabilityCheck(status=CapabilityState.ready))
    aggregate = HealthAggregator(
        service="megumin-companion-ai",
        version="test-version",
        providers=(provider,),
    )
    provider.name = "path.private"
    provider.required_for_readiness = False

    payload = asyncio.run(aggregate.readiness()).model_dump(mode="json", exclude_none=True)

    assert payload["checks"] == [{"name": "core", "status": "ready"}]


def test_empty_registry_is_live_ready_and_has_no_capabilities() -> None:
    aggregate = HealthAggregator(
        service="megumin-companion-ai",
        version="test-version",
        providers=(),
    )

    readiness = asyncio.run(aggregate.readiness()).model_dump(mode="json")
    capabilities = asyncio.run(aggregate.capabilities()).model_dump(mode="json")

    assert readiness["status"] == "ready" and readiness["checks"] == []
    assert capabilities["capabilities"] == []


def test_aggregator_rejects_invalid_timeout_duplicate_name_and_non_boolean_role() -> None:
    ready = CapabilityCheck(status=CapabilityState.ready)
    with pytest.raises(ValueError):
        HealthAggregator(
            service="megumin-companion-ai",
            version="C:\\Users\\private-user",
            providers=(),
        )
    with pytest.raises(ValueError, match="timeout"):
        HealthAggregator(
            service="megumin-companion-ai",
            version="test-version",
            providers=(),
            timeout_seconds=0,
        )
    duplicate = (_Provider("core", True, ready), _Provider("core", False, ready))
    with pytest.raises(ValueError, match="duplicate"):
        HealthAggregator(
            service="megumin-companion-ai",
            version="test-version",
            providers=duplicate,
        )
    invalid = _Provider("core", True, ready)
    invalid.required_for_readiness = "yes"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="boolean"):
        HealthAggregator(
            service="megumin-companion-ai",
            version="test-version",
            providers=(invalid,),
        )
