"""Fail-closed endpoint, proxy, and certificate policy for remote providers."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from urllib.parse import SplitResult, urlsplit


class ProviderTransportError(ValueError):
    """Content-free provider transport configuration failure."""


class EndpointKind(StrEnum):
    http = "http"
    websocket = "websocket"


@dataclass(frozen=True, slots=True)
class EndpointPolicy:
    loopback: bool
    tls: bool


def _fail(code: str) -> ProviderTransportError:
    return ProviderTransportError(code)


def _parsed_host(value: str, *, code: str) -> tuple[str, SplitResult]:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise _fail(code) from exc
    if (
        not host
        or "%" in host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _fail(code)
    return host.casefold(), parsed


def _is_explicit_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return isinstance(address, (IPv4Address, IPv6Address)) and address.is_loopback


def validate_endpoint(value: str, *, kind: EndpointKind) -> EndpointPolicy:
    """Require TLS remotely and classify cleartext loopback without DNS lookup."""

    host, parsed = _parsed_host(value, code="provider_endpoint_invalid")
    if kind is EndpointKind.http:
        cleartext, secure = "http", "https"
    else:
        cleartext, secure = "ws", "wss"
    if parsed.scheme not in {cleartext, secure}:
        raise _fail("provider_endpoint_invalid")
    loopback = _is_explicit_loopback(host)
    tls = parsed.scheme == secure
    if not tls and not loopback:
        raise _fail("provider_endpoint_invalid")
    return EndpointPolicy(loopback=loopback, tls=tls)


def validate_proxy_url(value: str) -> str:
    """Accept only explicit credential-free HTTP(S) proxies."""

    _host, parsed = _parsed_host(value, code="provider_proxy_invalid")
    if parsed.scheme not in {"http", "https"} or parsed.query or parsed.path not in {"", "/"}:
        raise _fail("provider_proxy_invalid")
    return value


def build_ssl_context(ca_bundle_path: Path | None) -> ssl.SSLContext:
    """Build verified platform trust, optionally extended by one explicit CA bundle."""

    try:
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        if ca_bundle_path is not None:
            if not ca_bundle_path.is_file():
                raise OSError
            context.load_verify_locations(cafile=str(ca_bundle_path))
    except (OSError, ssl.SSLError) as exc:
        raise _fail("provider_ca_invalid") from exc
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context
