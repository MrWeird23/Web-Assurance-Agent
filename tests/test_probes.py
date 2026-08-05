from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from triage_agent.probes import probe_url, resolve_addresses


async def test_probe_records_success_and_cloudflare_evidence() -> None:
    async def resolve(_host: str) -> set[str]:
        return {"104.21.10.20"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "104.21.10.20"
        assert request.headers["host"] == "monitor.example.com"
        assert request.extensions["sni_hostname"] == "monitor.example.com"
        assert request.headers["user-agent"].startswith("Web-Assurance-Agent/")
        return httpx.Response(
            200,
            headers={"server": "cloudflare", "cf-ray": "abc123-LIS"},
            text="Working normally",
        )

    resolver: Callable[[str], Awaitable[set[str]]] = resolve
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_url(
            "https://monitor.example.com/",
            allowed_hosts={"monitor.example.com"},
            client=client,
            resolver=resolver,
        )

    assert result.ok is True
    assert result.status_code == 200
    assert result.final_url == "https://monitor.example.com/"
    assert result.server == "cloudflare"
    assert result.cloudflare_ray == "abc123-LIS"
    assert result.error is None


async def test_probe_validates_and_follows_redirect_to_another_allowed_host() -> None:
    resolved_hosts: list[str] = []

    async def resolve(host: str) -> set[str]:
        resolved_hosts.append(host)
        return {"104.21.10.20"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(301, headers={"location": "https://www.example.com/"})
        return httpx.Response(200, text="Healthy")

    resolver: Callable[[str], Awaitable[set[str]]] = resolve
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_url(
            "https://example.com/",
            allowed_hosts={"example.com", "www.example.com"},
            client=client,
            resolver=resolver,
        )

    assert result.ok is True
    assert result.final_url == "https://www.example.com/"
    assert resolved_hosts == ["example.com", "www.example.com"]


async def test_probe_returns_evidence_for_network_failure() -> None:
    async def resolve(_host: str) -> set[str]:
        return {"104.21.10.20"}

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out", request=request)

    resolver: Callable[[str], Awaitable[set[str]]] = resolve
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_url(
            "https://example.com/",
            allowed_hosts={"example.com"},
            client=client,
            resolver=resolver,
        )

    assert result.ok is False
    assert result.status_code is None
    assert result.error == "ConnectTimeout: connection timed out"


async def test_probe_rejects_disallowed_host_before_dns_resolution() -> None:
    resolver_called = False

    async def resolve(_host: str) -> set[str]:
        nonlocal resolver_called
        resolver_called = True
        return {"104.21.10.20"}

    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_url(
            "https://internal.example/",
            allowed_hosts={"example.com"},
            client=client,
            resolver=resolve,
        )

    assert resolver_called is False
    assert result.ok is False
    assert result.error == "Unsafe target"


async def test_probe_returns_evidence_for_dns_failure() -> None:
    async def resolve(_host: str) -> set[str]:
        raise OSError("resolver unavailable")

    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_url(
            "https://example.com/",
            allowed_hosts={"example.com"},
            client=client,
            resolver=resolve,
        )

    assert result.ok is False
    assert result.status_code is None
    assert result.error == "DNS resolution failed"


async def test_probe_rejects_urls_httpx_cannot_construct_without_raising() -> None:
    async def resolve(_host: str) -> set[str]:
        return {"104.21.10.20"}

    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        control_result = await probe_url(
            "https://example.com/a\x00b",
            allowed_hosts={"example.com"},
            client=client,
            resolver=resolve,
        )
        unicode_result = await probe_url(
            "https://example.com/\ud800",
            allowed_hosts={"example.com"},
            client=client,
            resolver=resolve,
        )

    assert control_result.error == "Unsafe target"
    assert unicode_result.error == "Unsafe target"


async def test_probe_reports_redirect_limit_exhaustion_as_failure() -> None:
    async def resolve(_host: str) -> set[str]:
        return {"104.21.10.20"}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/next"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_url(
            "https://example.com/",
            allowed_hosts={"example.com"},
            client=client,
            resolver=resolve,
        )

    assert result.ok is False
    assert result.status_code == 302
    assert result.error == "Redirect limit exceeded"
    assert result.final_url.startswith("https://example.com/")


async def test_resolver_returns_all_ipv4_and_ipv6_addresses(monkeypatch: Any) -> None:
    def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            (2, 1, 6, "", ("104.21.10.20", 443)),
            (30, 1, 6, "", ("2606:4700::6815:a14", 443, 0, 0)),
        ]

    monkeypatch.setattr("triage_agent.probes.socket.getaddrinfo", fake_getaddrinfo)

    assert await resolve_addresses("example.com") == {
        "104.21.10.20",
        "2606:4700::6815:a14",
    }
