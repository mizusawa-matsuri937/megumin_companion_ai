"""W08 endpoint classification, explicit proxy, and TLS trust tests."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from app.provider_transport import (
    EndpointKind,
    ProviderTransportError,
    build_ssl_context,
    validate_endpoint,
    validate_proxy_url,
)


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("http://localhost:9880", EndpointKind.http),
        ("http://127.0.0.1:9880", EndpointKind.http),
        ("http://127.255.255.254:9880", EndpointKind.http),
        ("http://[::1]:9880", EndpointKind.http),
        ("http://[::ffff:127.0.0.1]:9880", EndpointKind.http),
        ("ws://localhost:8001", EndpointKind.websocket),
        ("ws://[::1]:8001", EndpointKind.websocket),
        ("https://provider.example", EndpointKind.http),
        ("wss://vts.example/socket", EndpointKind.websocket),
    ],
)
def test_endpoint_policy_accepts_only_explicit_loopback_cleartext(
    url: str, kind: EndpointKind
) -> None:
    policy = validate_endpoint(url, kind=kind)

    assert policy.loopback is (
        "localhost" in url or "127." in url or "::1" in url or "::ffff:127." in url
    )
    assert policy.tls is url.startswith(("https://", "wss://"))


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("http://provider.example", EndpointKind.http),
        ("ws://vts.example", EndpointKind.websocket),
        ("http://localhost.example:9880", EndpointKind.http),
        ("http://0.0.0.0:9880", EndpointKind.http),
        ("http://[::]:9880", EndpointKind.http),
        ("http://2130706433:9880", EndpointKind.http),
        ("http://[::1%25synthetic]:9880", EndpointKind.http),
        ("http://user:password@127.0.0.1:9880", EndpointKind.http),
        ("https://user:password@provider.example", EndpointKind.http),
        ("ftp://127.0.0.1/resource", EndpointKind.http),
        ("https://[not-an-ip]", EndpointKind.http),
        ("wss://", EndpointKind.websocket),
    ],
)
def test_endpoint_policy_rejects_remote_cleartext_userinfo_and_malformed_hosts(
    url: str, kind: EndpointKind
) -> None:
    with pytest.raises(ProviderTransportError, match="provider_endpoint_invalid") as captured:
        validate_endpoint(url, kind=kind)

    assert url not in str(captured.value)


@pytest.mark.parametrize("proxy", ["http://proxy.example:8080", "https://proxy.example"])
def test_proxy_must_be_explicit_credential_free_http_url(proxy: str) -> None:
    assert validate_proxy_url(proxy) == proxy


@pytest.mark.parametrize(
    "proxy",
    [
        "http://user:password@proxy.example:8080",
        "socks5://proxy.example:1080",
        "http://",
        "proxy.example:8080",
    ],
)
def test_proxy_rejects_credentials_and_unsupported_or_malformed_urls(proxy: str) -> None:
    with pytest.raises(ProviderTransportError, match="provider_proxy_invalid") as captured:
        validate_proxy_url(proxy)

    assert proxy not in str(captured.value)


def test_ssl_context_always_requires_hostname_and_certificate_validation(tmp_path: Path) -> None:
    context = build_ssl_context(None)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname

    missing = tmp_path / "private-enterprise-root.pem"
    with pytest.raises(ProviderTransportError, match="provider_ca_invalid") as captured:
        build_ssl_context(missing)

    assert str(missing) not in str(captured.value)
