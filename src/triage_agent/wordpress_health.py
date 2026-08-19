"""Read-only, site-specific WordPress administrative health collection."""

import ipaddress
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from triage_agent.security import UnsafeTargetError, validate_probe_url, validate_resolved_addresses

MAX_WORDPRESS_HEALTH_BYTES = 65_536
WordPressHealthErrorCode = Literal[
    "unsafe_target",
    "dns_resolution_failed",
    "request_failed",
    "unexpected_status",
    "response_too_large",
    "invalid_health_payload",
]


class StrictHealthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoreHealth(StrictHealthModel):
    version: StrictStr = Field(min_length=1, max_length=32)
    update_available: StrictBool


class PluginHealth(StrictHealthModel):
    slug: StrictStr = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    version: StrictStr = Field(min_length=1, max_length=32)
    active: StrictBool
    update_available: StrictBool


class ThemeHealth(StrictHealthModel):
    slug: StrictStr = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    version: StrictStr = Field(min_length=1, max_length=32)
    active: StrictBool
    update_available: StrictBool


class SiteHealth(StrictHealthModel):
    status: Literal["good", "recommended", "critical"]
    critical_tests: tuple[StrictStr, ...] = Field(max_length=100)


class CronHealth(StrictHealthModel):
    overdue: StrictInt = Field(ge=0, le=100_000)
    failing: StrictInt = Field(ge=0, le=100_000)


class RestApiHealth(StrictHealthModel):
    ok: StrictBool


class WordPressHealthPayload(StrictHealthModel):
    core: CoreHealth
    plugins: tuple[PluginHealth, ...] = Field(max_length=500)
    theme: ThemeHealth
    site_health: SiteHealth
    cron: CronHealth
    rest_api: RestApiHealth
    fatal_error_codes: tuple[StrictStr, ...] = Field(max_length=100)


@dataclass(frozen=True, slots=True)
class WordPressHealthResult:
    ok: bool
    error_code: WordPressHealthErrorCode | None
    core_version: str | None = None
    core_update_available: bool | None = None
    plugin_updates: tuple[str, ...] = ()
    theme_update_available: bool | None = None
    site_health_status: str | None = None
    critical_test_count: int | None = None
    overdue_cron_count: int | None = None
    failing_cron_count: int | None = None
    rest_api_ok: bool | None = None
    fatal_error_codes: tuple[str, ...] = ()
    raw_response: None = None


async def fetch_wordpress_health(
    *,
    url: str,
    allowed_hosts: set[str],
    token: str,
    client: httpx.AsyncClient,
    resolver: Callable[[str], Awaitable[set[str]]],
) -> WordPressHealthResult:
    try:
        parsed = validate_probe_url(url, allowed_hosts=allowed_hosts)
    except UnsafeTargetError:
        return _failure("unsafe_target")
    host = parsed.hostname
    if host is None:
        return _failure("unsafe_target")
    try:
        addresses = await resolver(host)
        validate_resolved_addresses(addresses)
    except UnsafeTargetError:
        return _failure("unsafe_target")
    except Exception:
        return _failure("dns_resolution_failed")

    response: httpx.Response | None = None
    for address in sorted(
        addresses,
        key=lambda value: (
            ipaddress.ip_address(value).version,
            int(ipaddress.ip_address(value)),
        ),
    ):
        request = client.build_request(
            "GET",
            httpx.URL(url).copy_with(host=address),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Host": host,
            },
            extensions={"sni_hostname": host},
        )
        try:
            response = await client.send(request, follow_redirects=False, stream=True)
            break
        except httpx.RequestError:
            continue
    if response is None:
        return _failure("request_failed")
    try:
        if response.status_code != 200:
            return _failure("unexpected_status")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_WORDPRESS_HEALTH_BYTES:
                    return _failure("response_too_large")
            except ValueError:
                return _failure("invalid_health_payload")

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > MAX_WORDPRESS_HEALTH_BYTES:
                return _failure("response_too_large")
            body.extend(chunk)
        try:
            payload = WordPressHealthPayload.model_validate(json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError):
            return _failure("invalid_health_payload")
    finally:
        await response.aclose()

    return WordPressHealthResult(
        ok=True,
        error_code=None,
        core_version=payload.core.version,
        core_update_available=payload.core.update_available,
        plugin_updates=tuple(plugin.slug for plugin in payload.plugins if plugin.update_available),
        theme_update_available=payload.theme.update_available,
        site_health_status=payload.site_health.status,
        critical_test_count=len(payload.site_health.critical_tests),
        overdue_cron_count=payload.cron.overdue,
        failing_cron_count=payload.cron.failing,
        rest_api_ok=payload.rest_api.ok,
        fatal_error_codes=payload.fatal_error_codes,
    )


def _failure(code: WordPressHealthErrorCode) -> WordPressHealthResult:
    return WordPressHealthResult(ok=False, error_code=code)
