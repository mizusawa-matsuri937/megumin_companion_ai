"""Content-free liveness, readiness, and capability aggregation."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

HEALTH_SCHEMA_VERSION: Literal[1] = 1
_SAFE_CODE_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
_SAFE_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$"


class _HealthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityState(StrEnum):
    ready = "ready"
    degraded = "degraded"
    unavailable = "unavailable"
    disabled = "disabled"
    unknown = "unknown"


class CapabilityCheck(_HealthModel):
    """The complete allowlist accepted from one health provider."""

    status: CapabilityState
    error_code: str | None = Field(default=None, pattern=_SAFE_CODE_PATTERN)


class CapabilityReport(CapabilityCheck):
    name: str = Field(pattern=_SAFE_CODE_PATTERN)


class LivenessPayload(_HealthModel):
    schema_version: Literal[1] = HEALTH_SCHEMA_VERSION
    status: Literal["live"] = "live"
    service: str = Field(pattern=_SAFE_CODE_PATTERN)
    version: str = Field(pattern=_SAFE_VERSION_PATTERN)


class ReadinessPayload(_HealthModel):
    schema_version: Literal[1] = HEALTH_SCHEMA_VERSION
    status: Literal["ready", "not_ready"]
    service: str = Field(pattern=_SAFE_CODE_PATTERN)
    version: str = Field(pattern=_SAFE_VERSION_PATTERN)
    checks: tuple[CapabilityReport, ...]


class CapabilitiesPayload(_HealthModel):
    schema_version: Literal[1] = HEALTH_SCHEMA_VERSION
    status: Literal["ok"] = "ok"
    service: str = Field(pattern=_SAFE_CODE_PATTERN)
    version: str = Field(pattern=_SAFE_VERSION_PATTERN)
    capabilities: tuple[CapabilityReport, ...]


@runtime_checkable
class HealthProvider(Protocol):
    """Generic boundary implemented by current and future runtime components."""

    name: str
    required_for_readiness: bool

    async def check_health(self) -> CapabilityCheck: ...


@dataclass(frozen=True, slots=True)
class _RegisteredProvider:
    name: str
    required_for_readiness: bool
    provider: HealthProvider


class HealthAggregator:
    """Aggregate bounded provider checks without reflecting provider exceptions."""

    def __init__(
        self,
        *,
        service: str,
        version: str,
        providers: Sequence[HealthProvider],
        timeout_seconds: float = 0.25,
    ) -> None:
        if timeout_seconds <= 0.0 or timeout_seconds > 5.0:
            raise ValueError("health timeout must be within (0, 5] seconds")
        LivenessPayload(service=service, version=version)
        names: set[str] = set()
        validated: list[_RegisteredProvider] = []
        for provider in providers:
            name = provider.name
            required = provider.required_for_readiness
            CapabilityReport(name=name, status=CapabilityState.unknown)
            if not isinstance(required, bool):
                raise ValueError("health provider readiness flag must be boolean")
            if name in names:
                raise ValueError("duplicate health provider")
            names.add(name)
            validated.append(
                _RegisteredProvider(
                    name=name,
                    required_for_readiness=required,
                    provider=provider,
                )
            )
        self._service = service
        self._version = version
        self._providers = tuple(validated)
        self._timeout_seconds = timeout_seconds

    async def liveness(self) -> LivenessPayload:
        return LivenessPayload(service=self._service, version=self._version)

    async def readiness(self) -> ReadinessPayload:
        required = tuple(item for item in self._providers if item.required_for_readiness)
        reports = await self._collect(required)
        ready = all(item.status is CapabilityState.ready for item in reports)
        return ReadinessPayload(
            status="ready" if ready else "not_ready",
            service=self._service,
            version=self._version,
            checks=reports,
        )

    async def capabilities(self) -> CapabilitiesPayload:
        return CapabilitiesPayload(
            service=self._service,
            version=self._version,
            capabilities=await self._collect(self._providers),
        )

    async def _collect(
        self,
        providers: Sequence[_RegisteredProvider],
    ) -> tuple[CapabilityReport, ...]:
        if not providers:
            return ()
        return tuple(await asyncio.gather(*(self._check(item) for item in providers)))

    async def _check(self, registered: _RegisteredProvider) -> CapabilityReport:
        try:
            raw: Any = await asyncio.wait_for(
                registered.provider.check_health(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return self._failure(registered.name, "health_provider_timeout")
        except Exception:
            return self._failure(registered.name, "health_provider_failed")
        try:
            checked = CapabilityCheck.model_validate(raw)
        except Exception:
            return self._failure(registered.name, "health_provider_invalid")
        return CapabilityReport(
            name=registered.name,
            status=checked.status,
            error_code=checked.error_code,
        )

    @staticmethod
    def _failure(name: str, error_code: str) -> CapabilityReport:
        return CapabilityReport(
            name=name,
            status=CapabilityState.unavailable,
            error_code=error_code,
        )
