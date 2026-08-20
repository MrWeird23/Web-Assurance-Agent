import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import urljoin

import httpx

from triage_agent import __version__
from triage_agent.security import (
    UnsafeTargetError,
    validate_probe_url,
    validate_resolved_addresses,
)

MAX_REDIRECTS = 5


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    status_code: int | None
    latency_ms: int | None
    final_url: str
    error: str | None
    server: str | None
    cloudflare_ray: str | None = None


async def resolve_addresses(host: str) -> set[str]:
    records = await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        443,
        type=socket.SOCK_STREAM,
    )
    return {str(record[4][0]) for record in records}


async def probe_url(
    url: str,
    *,
    allowed_hosts: set[str],
    client: httpx.AsyncClient,
    resolver: Callable[[str], Awaitable[set[str]]],
) -> ProbeResult:
    started = perf_counter()
    current_url = url
    response: httpx.Response | None = None
    for redirect_count in range(MAX_REDIRECTS + 1):
        try:
            parsed = validate_probe_url(current_url, allowed_hosts=allowed_hosts)
        except UnsafeTargetError:
            return _failed_result(started, current_url, "Unsafe target")

        host = parsed.hostname
        if host is None:
            return _failed_result(started, current_url, "Unsafe target")
        try:
            addresses = await resolver(host)
        except Exception:
            return _failed_result(started, current_url, "DNS resolution failed")
        try:
            validate_resolved_addresses(addresses)
        except UnsafeTargetError:
            return _failed_result(started, current_url, "Unsafe target")

        response = None
        last_request_error: httpx.RequestError | None = None
        for address in sorted(
            addresses,
            key=lambda value: (
                ipaddress.ip_address(value).version,
                int(ipaddress.ip_address(value)),
            ),
        ):
            request_url = httpx.URL(current_url).copy_with(host=address)
            request = client.build_request(
                "GET",
                request_url,
                headers={
                    "Host": host,
                    "User-Agent": f"Web-Assurance-Agent/{__version__}",
                },
                extensions={"sni_hostname": host},
            )
            try:
                response = await client.send(request, follow_redirects=False)
                break
            except httpx.RequestError as exc:
                last_request_error = exc
        if response is None and last_request_error is not None:
            latency_ms = round((perf_counter() - started) * 1000)
            return ProbeResult(
                ok=False,
                status_code=None,
                latency_ms=latency_ms,
                final_url=current_url,
                error=f"{type(last_request_error).__name__}: {last_request_error}",
                server=None,
            )
        if response is None:
            return _failed_result(started, current_url, "Probe request failed")
        location = response.headers.get("location")
        if not response.is_redirect or location is None:
            break
        redirect_url = urljoin(current_url, location)
        try:
            redirect_parsed = validate_probe_url(
                redirect_url,
                allowed_hosts=allowed_hosts,
            )
        except UnsafeTargetError:
            return _failed_result(
                started,
                current_url,
                "Unsafe redirect target",
                status_code=response.status_code,
            )
        if redirect_count == MAX_REDIRECTS:
            redirect_host = redirect_parsed.hostname
            if redirect_host is None:
                return _failed_result(started, current_url, "Unsafe redirect target")
            try:
                redirect_addresses = await resolver(redirect_host)
                validate_resolved_addresses(redirect_addresses)
            except (Exception, UnsafeTargetError):
                return _failed_result(
                    started,
                    current_url,
                    "Unsafe redirect target",
                    status_code=response.status_code,
                )
            return _failed_result(
                started,
                current_url,
                "Redirect limit exceeded",
                status_code=response.status_code,
            )
        current_url = redirect_url
    if response is None:
        raise RuntimeError("Probe did not issue a request")
    latency_ms = round((perf_counter() - started) * 1000)
    ok = 200 <= response.status_code < 400
    return ProbeResult(
        ok=ok,
        status_code=response.status_code,
        latency_ms=latency_ms,
        final_url=current_url,
        error=None if ok else f"HTTP {response.status_code}",
        server=response.headers.get("server"),
        cloudflare_ray=response.headers.get("cf-ray"),
    )


def _failed_result(
    started: float,
    final_url: str,
    error: str,
    *,
    status_code: int | None = None,
) -> ProbeResult:
    return ProbeResult(
        ok=False,
        status_code=status_code,
        latency_ms=round((perf_counter() - started) * 1000),
        final_url=final_url,
        error=error,
        server=None,
    )
