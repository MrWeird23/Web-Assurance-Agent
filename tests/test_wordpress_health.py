import json
from collections.abc import AsyncIterator

import httpx
from httpx import AsyncByteStream

from triage_agent.wordpress_health import fetch_wordpress_health


async def public_resolver(_host: str) -> set[str]:
    return {"93.184.216.34"}


class CountingStream(AsyncByteStream):
    def __init__(self, chunks: int, chunk_size: int) -> None:
        self.chunks = chunks
        self.chunk_size = chunk_size
        self.chunks_read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.chunks):
            if self.chunks_read < self.chunks:
                self.chunks_read += 1
            yield b"x" * self.chunk_size


async def test_fetches_typed_read_only_health_without_retaining_raw_text() -> None:
    payload = {
        "core": {"version": "6.8.1", "update_available": False},
        "plugins": [
            {"slug": "contact-form-7", "version": "6.0", "active": True, "update_available": True}
        ],
        "theme": {"slug": "techx", "version": "1.2.0", "active": True, "update_available": False},
        "site_health": {"status": "good", "critical_tests": []},
        "cron": {"overdue": 0, "failing": 0},
        "rest_api": {"ok": True},
        "fatal_error_codes": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        assert request.headers["authorization"] == "Bearer site-specific-secret"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_wordpress_health(
            url="https://example.com/wp-json/techx-monitor/v1/health",
            allowed_hosts={"example.com"},
            token="site-specific-secret",
            client=client,
            resolver=public_resolver,
        )

    assert result.ok is True
    assert result.core_version == "6.8.1"
    assert result.plugin_updates == ("contact-form-7",)
    assert result.site_health_status == "good"
    assert result.raw_response is None


async def test_rejects_unsafe_target_before_request() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_wordpress_health(
            url="https://localhost/wp-json/techx-monitor/v1/health",
            allowed_hosts={"example.com"},
            token="site-specific-secret",
            client=client,
            resolver=public_resolver,
        )

    assert result.ok is False
    assert result.error_code == "unsafe_target"
    assert calls == 0


async def test_rejects_oversized_response_without_json_parsing() -> None:
    body = json.dumps({"padding": "x" * 70_000}).encode()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_wordpress_health(
            url="https://example.com/wp-json/techx-monitor/v1/health",
            allowed_hosts={"example.com"},
            token="site-specific-secret",
            client=client,
            resolver=public_resolver,
        )

    assert result.ok is False
    assert result.error_code == "response_too_large"


async def test_rejects_oversized_stream_before_buffering_entire_body() -> None:
    stream = CountingStream(chunks=100, chunk_size=4096)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_wordpress_health(
            url="https://example.com/wp-json/techx-monitor/v1/health",
            allowed_hosts={"example.com"},
            token="site-specific-secret",
            client=client,
            resolver=public_resolver,
        )

    assert result.error_code == "response_too_large"
    assert stream.chunks_read == 17


async def test_rejects_oversized_declared_content_length_without_reading_body() -> None:
    stream = CountingStream(chunks=1, chunk_size=2)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "70000"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_wordpress_health(
            url="https://example.com/wp-json/techx-monitor/v1/health",
            allowed_hosts={"example.com"},
            token="site-specific-secret",
            client=client,
            resolver=public_resolver,
        )

    assert result.error_code == "response_too_large"
    assert stream.chunks_read == 0


async def test_rejects_malformed_or_extra_health_fields() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"core": {"version": "6.8"}, "secret": "must-not-pass"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_wordpress_health(
            url="https://example.com/wp-json/techx-monitor/v1/health",
            allowed_hosts={"example.com"},
            token="site-specific-secret",
            client=client,
            resolver=public_resolver,
        )

    assert result.ok is False
    assert result.error_code == "invalid_health_payload"
