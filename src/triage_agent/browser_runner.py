import asyncio
import hashlib
import ipaddress
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from http.cookies import SimpleCookie
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.async_api import (
    ConsoleMessage,
    Route,
    WebSocketRoute,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from triage_agent import __version__
from triage_agent.application_signatures import detect_application_failure_codes
from triage_agent.browser_checks import (
    BrowserEvidence,
    ResourceFailure,
    ScreenshotArtifact,
    SelectorResult,
    TextResult,
    Viewport,
)
from triage_agent.manifests import PageManifest, ViewportManifest
from triage_agent.security import (
    UnsafeTargetError,
    validate_probe_url,
    validate_resolved_addresses,
)


@dataclass(frozen=True, slots=True)
class AuthorizedBrowserTarget:
    url: str
    host: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    domain_initial_dot: bool
    path: str
    expires: int | None
    secure: bool
    http_only: bool
    same_site: str | None


@dataclass(frozen=True, slots=True)
class BrowserResourceResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    final_url: str
    cookies: tuple[BrowserCookie, ...] = ()


MAX_BROWSER_RESOURCE_BYTES = 20 * 1024 * 1024
MAX_BROWSER_TOTAL_BYTES = 64 * 1024 * 1024
MAX_BROWSER_REQUESTS = 64
MAX_BROWSER_CONCURRENT_REQUESTS = 8
MAX_BROWSER_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_BROWSER_SCREENSHOT_PIXELS = 16 * 1024 * 1024


class BrowserFetchError(RuntimeError):
    pass


@dataclass(slots=True)
class BrowserRunBudget:
    max_requests: int = MAX_BROWSER_REQUESTS
    max_total_bytes: int = MAX_BROWSER_TOTAL_BYTES
    max_concurrent_requests: int = MAX_BROWSER_CONCURRENT_REQUESTS
    request_count: int = 0
    total_bytes: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.max_concurrent_requests)

    async def reserve_request(self) -> None:
        async with self._lock:
            if self.request_count >= self.max_requests:
                raise BrowserFetchError("Browser request budget exceeded")
            self.request_count += 1

    async def consume_bytes(self, size: int) -> None:
        async with self._lock:
            if self.total_bytes + size > self.max_total_bytes:
                raise BrowserFetchError("Browser byte budget exceeded")
            self.total_bytes += size


class BrowserRequest(Protocol):
    url: str
    method: str


class BrowserRoute(Protocol):
    @property
    def request(self) -> BrowserRequest: ...

    async def abort(self, error_code: str = "failed") -> None: ...

    async def fulfill(
        self,
        *,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None: ...


MAX_BROWSER_REDIRECTS = 10
DEFAULT_NAVIGATION_TIMEOUT_MS = 15_000
DEFAULT_TOTAL_TIMEOUT_MS = 30_000
MAX_BROWSER_ERROR_RECORDS = 100
MAX_BROWSER_ERROR_LENGTH = 500
MAX_BROWSER_RESOURCE_FAILURES = 100
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def build_browser_context_options(viewport: ViewportManifest) -> dict[str, object]:
    return {
        "viewport": {"width": viewport.width, "height": viewport.height},
        "device_scale_factor": viewport.device_scale_factor,
        "user_agent": f"Web-Assurance-Agent/{__version__}",
        "locale": "en-US",
        "timezone_id": "UTC",
        "color_scheme": "light",
        "reduced_motion": "reduce",
        "service_workers": "block",
        "accept_downloads": False,
        "ignore_https_errors": False,
    }


async def authorize_browser_url(
    url: str,
    *,
    allowed_hosts: set[str],
    resolver: Callable[[str], Awaitable[set[str]]],
) -> AuthorizedBrowserTarget:
    parsed = validate_probe_url(url, allowed_hosts=allowed_hosts)
    host = parsed.hostname
    if host is None:
        raise UnsafeTargetError("Browser target has no hostname")
    try:
        addresses = await resolver(host)
    except Exception as exc:
        raise BrowserFetchError("Browser target resolution failed") from exc
    validate_resolved_addresses(addresses)
    return AuthorizedBrowserTarget(
        url=url,
        host=host,
        addresses=tuple(sorted(addresses)),
    )


async def _fetch_browser_resource_unbounded(
    url: str,
    *,
    allowed_hosts: set[str],
    resolver: Callable[[str], Awaitable[set[str]]],
    client: httpx.AsyncClient | None = None,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    request_headers: Mapping[str, str] | None = None,
    budget: BrowserRunBudget | None = None,
) -> BrowserResourceResponse:
    target = await authorize_browser_url(
        url,
        allowed_hosts=allowed_hosts,
        resolver=resolver,
    )
    forwarded_headers = {
        name: value
        for name, value in (request_headers or {}).items()
        if name.lower()
        in {
            "accept",
            "accept-language",
            "cache-control",
            "cookie",
            "if-match",
            "if-modified-since",
            "if-none-match",
            "if-range",
            "if-unmodified-since",
            "origin",
            "pragma",
            "range",
            "referer",
            "sec-fetch-dest",
            "sec-fetch-mode",
            "sec-fetch-site",
            "sec-fetch-user",
        }
    }
    forwarded_headers.update(
        {
            "Accept-Encoding": "identity",
            "Host": target.host,
            "User-Agent": f"Web-Assurance-Agent/{__version__}",
        }
    )
    if client is not None and not isinstance(
        getattr(client, "_transport", None),
        httpx.MockTransport,
    ):
        raise BrowserFetchError("Shared browser HTTP clients are not permitted")

    async def consume_response(response: httpx.Response) -> BrowserResourceResponse:
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_BROWSER_RESOURCE_BYTES:
                    raise BrowserFetchError("Browser resource exceeded the size limit")
                if budget is not None:
                    await budget.consume_bytes(len(chunk))
                body.extend(chunk)
            location = response.headers.get("location")
            validated_location: str | None = None
            if response.status_code in _REDIRECT_STATUSES and location:
                validated_location = urljoin(url, location)
                validate_probe_url(validated_location, allowed_hosts=allowed_hosts)
            headers = tuple(
                (name, value)
                for name, value in response.headers.multi_items()
                if name.lower() not in _HOP_BY_HOP_HEADERS
                and not (validated_location is not None and name.lower() == "location")
            )
            if validated_location is not None:
                headers += (("location", validated_location),)
            return BrowserResourceResponse(
                status_code=response.status_code,
                headers=headers,
                body=bytes(body),
                final_url=url,
            )
        finally:
            await response.aclose()

    last_error: httpx.RequestError | None = None
    for address in sorted(
        target.addresses,
        key=lambda value: (
            ipaddress.ip_address(value).version,
            int(ipaddress.ip_address(value)),
        ),
    ):
        request_url = httpx.URL(url).copy_with(host=address)
        request = httpx.Request(
            "GET",
            request_url,
            headers=forwarded_headers,
            extensions={"sni_hostname": target.host},
        )
        try:
            if client is not None:
                response = await client.send(
                    request,
                    follow_redirects=False,
                    stream=True,
                )
                return await consume_response(response)
            transport = (
                transport_factory()
                if transport_factory is not None
                else httpx.AsyncHTTPTransport(
                    retries=0,
                    limits=httpx.Limits(
                        max_connections=1,
                        max_keepalive_connections=0,
                    ),
                )
            )
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                http2=False,
                trust_env=False,
            ) as isolated_client:
                response = await isolated_client.send(
                    request,
                    follow_redirects=False,
                    stream=True,
                )
                return await consume_response(response)
        except httpx.RequestError as exc:
            last_error = exc
    raise BrowserFetchError("Browser resource request failed") from last_error


async def fetch_browser_resource(
    url: str,
    *,
    allowed_hosts: set[str],
    resolver: Callable[[str], Awaitable[set[str]]],
    client: httpx.AsyncClient | None = None,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    request_headers: Mapping[str, str] | None = None,
    budget: BrowserRunBudget | None = None,
) -> BrowserResourceResponse:
    if budget is None:
        return await _fetch_browser_resource_unbounded(
            url,
            allowed_hosts=allowed_hosts,
            resolver=resolver,
            client=client,
            transport_factory=transport_factory,
            request_headers=request_headers,
        )
    await budget.reserve_request()
    async with budget.semaphore:
        return await _fetch_browser_resource_unbounded(
            url,
            allowed_hosts=allowed_hosts,
            resolver=resolver,
            client=client,
            transport_factory=transport_factory,
            request_headers=request_headers,
            budget=budget,
        )


def _response_redirect_location(response: BrowserResourceResponse) -> str | None:
    if response.status_code not in _REDIRECT_STATUSES:
        return None
    return next(
        (value for name, value in response.headers if name.lower() == "location"),
        None,
    )


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.hostname is None:
        raise BrowserFetchError("Browser redirect origin was invalid")
    return parsed.scheme, parsed.hostname, parsed.port or 443


def _browser_cookies(cookies: httpx.Cookies) -> tuple[BrowserCookie, ...]:
    def nonstandard_attribute(
        cookie: Any,
        name: str,
    ) -> tuple[bool, str | None]:
        attributes = cast(
            Mapping[str, str | None],
            vars(cookie).get("_rest", {}),
        )
        for attribute_name, value in attributes.items():
            if attribute_name.casefold() == name.casefold():
                return True, value
        return False, None

    def convert(cookie: Any) -> BrowserCookie:
        http_only, _http_only_value = nonstandard_attribute(cookie, "HttpOnly")
        _has_same_site, same_site = nonstandard_attribute(cookie, "SameSite")
        return BrowserCookie(
            name=cookie.name,
            value=cookie.value or "",
            domain=cookie.domain,
            domain_initial_dot=cookie.domain_initial_dot,
            path=cookie.path,
            expires=cookie.expires,
            secure=cookie.secure,
            http_only=http_only,
            same_site=same_site,
        )

    return tuple(
        convert(cookie)
        for cookie in cookies.jar
    )


def _playwright_cookies(cookies: tuple[BrowserCookie, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for cookie in cookies:
        item: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "secure": cookie.secure,
            "httpOnly": cookie.http_only,
        }
        if cookie.domain_initial_dot:
            item["domain"] = cookie.domain
            item["path"] = cookie.path
        else:
            item["url"] = f"https://{cookie.domain}{cookie.path}"
        if cookie.expires is not None:
            item["expires"] = float(cookie.expires)
        normalized_same_site = {
            "lax": "Lax",
            "none": "None",
            "strict": "Strict",
        }.get(cookie.same_site.casefold() if cookie.same_site is not None else "")
        if normalized_same_site is not None:
            item["sameSite"] = normalized_same_site
        result.append(item)
    return result


def _merge_cookie_headers(existing: str | None, scoped: str | None) -> str | None:
    merged = SimpleCookie()
    if existing:
        merged.load(existing)
    if scoped:
        merged.load(scoped)
    if not merged:
        return None
    return "; ".join(f"{name}={morsel.coded_value}" for name, morsel in merged.items())


async def fetch_browser_redirect_chain(
    url: str,
    *,
    allowed_hosts: set[str],
    resolver: Callable[[str], Awaitable[set[str]]],
    client: httpx.AsyncClient | None = None,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    request_headers: Mapping[str, str] | None = None,
    allow_cross_origin: bool,
    budget: BrowserRunBudget | None = None,
) -> BrowserResourceResponse:
    current_url = url
    original_origin = _url_origin(url)
    redirect_cookies = httpx.Cookies()
    for redirect_count in range(MAX_BROWSER_REDIRECTS + 1):
        hop_headers = dict(request_headers or {})
        cookie_request = httpx.Request("GET", current_url)
        redirect_cookies.set_cookie_header(cookie_request)
        scoped_cookie = cookie_request.headers.get("cookie")
        existing_cookie = next(
            (
                value
                for name, value in hop_headers.items()
                if name.lower() == "cookie"
            ),
            None,
        )
        merged_cookie = _merge_cookie_headers(existing_cookie, scoped_cookie)
        for name in tuple(hop_headers):
            if name.lower() == "cookie":
                del hop_headers[name]
        if merged_cookie is not None:
            hop_headers["cookie"] = merged_cookie
        response = await fetch_browser_resource(
            current_url,
            allowed_hosts=allowed_hosts,
            resolver=resolver,
            client=client,
            transport_factory=transport_factory,
            request_headers=hop_headers,
            budget=budget,
        )
        cookie_response = httpx.Response(
            response.status_code,
            headers=response.headers,
            request=httpx.Request("GET", current_url),
        )
        redirect_cookies.extract_cookies(cookie_response)
        location = _response_redirect_location(response)
        if location is None:
            return BrowserResourceResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=response.body,
                final_url=current_url,
                cookies=_browser_cookies(redirect_cookies),
            )
        if redirect_count >= MAX_BROWSER_REDIRECTS:
            raise BrowserFetchError("Browser redirect limit exceeded")
        if not allow_cross_origin and _url_origin(location) != original_origin:
            raise BrowserFetchError("Cross-origin browser redirect was blocked")
        current_url = location
    raise BrowserFetchError("Browser redirect limit exceeded")


async def handle_browser_route(
    route: BrowserRoute,
    *,
    allowed_hosts: set[str],
    resolver: Callable[[str], Awaitable[set[str]]],
    client: httpx.AsyncClient | None = None,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    request_headers: Mapping[str, str] | None = None,
    budget: BrowserRunBudget | None = None,
    cookie_sink: Callable[[tuple[BrowserCookie, ...]], Awaitable[None]] | None = None,
) -> BrowserResourceResponse | None:
    if route.request.method != "GET":
        await route.abort("blockedbyclient")
        return None
    try:
        response = await fetch_browser_redirect_chain(
            route.request.url,
            allowed_hosts=allowed_hosts,
            resolver=resolver,
            client=client,
            transport_factory=transport_factory,
            request_headers=request_headers,
            allow_cross_origin=False,
            budget=budget,
        )
        if response.cookies and cookie_sink is not None:
            await cookie_sink(response.cookies)
    except Exception:
        await route.abort("blockedbyclient")
        return None
    await route.fulfill(
        status=response.status_code,
        headers=_playwright_headers(response.headers),
        body=response.body,
    )
    return response


def _resource_origin(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    display_host = f"[{host}]" if ":" in host else host
    return f"https://{display_host}/"


def _playwright_headers(headers: tuple[tuple[str, str], ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers:
        normalized_name = name.lower()
        if normalized_name == "set-cookie" and normalized_name in result:
            result[normalized_name] = f"{result[normalized_name]}\n{value}"
        elif normalized_name in result:
            result[normalized_name] = f"{result[normalized_name]}, {value}"
        else:
            result[normalized_name] = value
    return result


def _redirect_depth(request: Any) -> int:
    depth = 0
    current = request.redirected_from
    while current is not None:
        depth += 1
        if depth > MAX_BROWSER_REDIRECTS:
            return depth
        current = current.redirected_from
    return depth


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if (
        len(content) < 24
        or content[:8] != _PNG_SIGNATURE
        or content[12:16] != b"IHDR"
    ):
        raise BrowserFetchError("Browser screenshot was not a valid PNG")
    width = int.from_bytes(content[16:20], byteorder="big")
    height = int.from_bytes(content[20:24], byteorder="big")
    if width <= 0 or height <= 0:
        raise BrowserFetchError("Browser screenshot dimensions were invalid")
    return width, height


def _validate_screenshot_viewport(viewport: ViewportManifest) -> None:
    if viewport.width * viewport.height > MAX_BROWSER_SCREENSHOT_PIXELS:
        raise BrowserFetchError("Browser screenshot exceeded pixel limit")


def _incomplete_evidence(
    *,
    page: PageManifest,
    viewport: ViewportManifest,
    duration_ms: int,
    timed_out: bool,
) -> BrowserEvidence:
    return BrowserEvidence(
        page_id=page.id,
        requested_url=page.url,
        final_url=page.url,
        viewport=Viewport(
            width=viewport.width,
            height=viewport.height,
            device_scale_factor=viewport.device_scale_factor,
        ),
        device_profile=viewport.id,
        document_status=None,
        title="",
        required_text_results=tuple(
            TextResult(value=text, found=False) for text in page.required_text
        ),
        required_selector_results=tuple(
            SelectorResult(
                selector=selector,
                found=False,
                visible=False,
                width=0,
                height=0,
            )
            for selector in page.required_selectors
        ),
        forbidden_text_matches=(),
        application_failure_codes=(),
        console_errors=(),
        page_exceptions=(),
        resource_failures=(),
        duration_ms=duration_ms,
        timed_out=timed_out,
        screenshot=None,
    )


@dataclass(slots=True)
class PlaywrightBrowserRunner:
    resolver: Callable[[str], Awaitable[set[str]]]
    client: httpx.AsyncClient | None = None
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None
    artifact_directory: Path | None = None
    navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS
    total_timeout_ms: int = DEFAULT_TOTAL_TIMEOUT_MS

    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence:
        started = perf_counter()
        try:
            return await self._run_with_deadline(
                page=page,
                viewport=viewport,
                allowed_hosts=allowed_hosts,
            )
        except TimeoutError:
            return _incomplete_evidence(
                page=page,
                viewport=viewport,
                duration_ms=round((perf_counter() - started) * 1000),
                timed_out=True,
            )
        except (BrowserFetchError, OSError, PlaywrightError, UnsafeTargetError):
            return _incomplete_evidence(
                page=page,
                viewport=viewport,
                duration_ms=round((perf_counter() - started) * 1000),
                timed_out=False,
            )

    async def _run_with_deadline(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence:
        started = perf_counter()
        async with asyncio.timeout(self.total_timeout_ms / 1000):
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--host-resolver-rules=MAP * ~NOTFOUND",
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--disable-sync",
                        "--metrics-recording-only",
                        "--no-first-run",
                    ],
                )
                try:
                    context_options = cast(
                        dict[str, Any],
                        build_browser_context_options(viewport),
                    )
                    context = await browser.new_context(**context_options)
                    route_tasks: set[asyncio.Task[None]] = set()
                    try:
                        resource_failures: list[ResourceFailure] = []
                        run_budget = BrowserRunBudget()
                        initial_response = await fetch_browser_redirect_chain(
                            page.url,
                            allowed_hosts=allowed_hosts,
                            resolver=self.resolver,
                            client=self.client,
                            transport_factory=self.transport_factory,
                            allow_cross_origin=True,
                            budget=run_budget,
                        )
                        document_final_url = initial_response.final_url
                        entry_response: BrowserResourceResponse | None = initial_response
                        if initial_response.cookies:
                            await context.add_cookies(
                                cast(Any, _playwright_cookies(initial_response.cookies))
                            )

                        async def seed_redirect_cookies(
                            cookies: tuple[BrowserCookie, ...],
                        ) -> None:
                            await context.add_cookies(cast(Any, _playwright_cookies(cookies)))

                        async def route_handler(route: Route) -> None:
                            nonlocal entry_response
                            task = cast(asyncio.Task[None] | None, asyncio.current_task())
                            if task is not None:
                                route_tasks.add(task)
                            try:
                                request = route.request
                                if _redirect_depth(request) > MAX_BROWSER_REDIRECTS:
                                    await route.abort("blockedbyclient")
                                    resource_response = None
                                elif (
                                    request.resource_type == "document"
                                    and request.url == document_final_url
                                    and entry_response is not None
                                ):
                                    resource_response = entry_response
                                    entry_response = None
                                    await route.fulfill(
                                        status=resource_response.status_code,
                                        headers=_playwright_headers(
                                            resource_response.headers
                                        ),
                                        body=resource_response.body,
                                    )
                                else:
                                    request_headers = await request.all_headers()
                                    resource_response = await handle_browser_route(
                                        cast(BrowserRoute, route),
                                        allowed_hosts=allowed_hosts,
                                        resolver=self.resolver,
                                        client=self.client,
                                        transport_factory=self.transport_factory,
                                        request_headers=request_headers,
                                        budget=run_budget,
                                        cookie_sink=seed_redirect_cookies,
                                    )
                                if resource_response is None:
                                    if (
                                        request.resource_type != "document"
                                        and len(resource_failures)
                                        < MAX_BROWSER_RESOURCE_FAILURES
                                    ):
                                        failed_path = urlsplit(request.url).path
                                        if not any(
                                            fnmatchcase(failed_path, pattern)
                                            for pattern in page.ignored_resource_patterns
                                        ):
                                            resource_failures.append(
                                                ResourceFailure(
                                                    url=_resource_origin(request.url),
                                                    status_code=None,
                                                    resource_type=request.resource_type,
                                                    critical=any(
                                                        fnmatchcase(
                                                            failed_path,
                                                            pattern,
                                                        )
                                                        for pattern in (
                                                            page.critical_resource_patterns
                                                        )
                                                    ),
                                                )
                                            )
                                    return
                                if (
                                    resource_response.status_code < 400
                                    or request.resource_type == "document"
                                    or len(resource_failures)
                                    >= MAX_BROWSER_RESOURCE_FAILURES
                                ):
                                    return
                                path = urlsplit(request.url).path
                                if any(
                                    fnmatchcase(path, pattern)
                                    for pattern in page.ignored_resource_patterns
                                ):
                                    return
                                resource_failures.append(
                                    ResourceFailure(
                                        url=_resource_origin(request.url),
                                        status_code=resource_response.status_code,
                                        resource_type=request.resource_type,
                                        critical=any(
                                            fnmatchcase(path, pattern)
                                            for pattern in page.critical_resource_patterns
                                        ),
                                    )
                                )
                            finally:
                                if task is not None:
                                    route_tasks.discard(task)

                        async def web_socket_handler(web_socket: WebSocketRoute) -> None:
                            await web_socket.close(code=1008, reason="Blocked by policy")

                        await context.route("**/*", route_handler)
                        await context.route_web_socket("**/*", web_socket_handler)
                        browser_page = await context.new_page()
                        console_errors: list[str] = []
                        page_exceptions: list[str] = []

                        def console_handler(message: ConsoleMessage) -> None:
                            if (
                                message.type == "error"
                                and len(console_errors) < MAX_BROWSER_ERROR_RECORDS
                            ):
                                console_errors.append("console_error")

                        def page_error_handler(error: PlaywrightError) -> None:
                            if len(page_exceptions) < MAX_BROWSER_ERROR_RECORDS:
                                page_exceptions.append("page_exception")

                        browser_page.on("console", console_handler)
                        browser_page.on("pageerror", page_error_handler)
                        response = await browser_page.goto(
                            document_final_url,
                            wait_until="networkidle",
                            timeout=self.navigation_timeout_ms,
                        )
                        await browser_page.add_style_tag(
                            content=(
                                "*,*::before,*::after{"
                                "animation:none!important;"
                                "transition:none!important;"
                                "caret-color:transparent!important;"
                                "scroll-behavior:auto!important}"
                            )
                        )
                        body_text = await browser_page.locator("body").inner_text()
                        selector_results: list[SelectorResult] = []
                        for selector in page.required_selectors:
                            locator = browser_page.locator(selector).first
                            found = await locator.count() > 0
                            visible = found and await locator.is_visible()
                            box = await locator.bounding_box() if found else None
                            selector_results.append(
                                SelectorResult(
                                    selector=selector,
                                    found=found,
                                    visible=visible,
                                    width=float(box["width"]) if box is not None else 0,
                                    height=float(box["height"]) if box is not None else 0,
                                )
                            )
                        screenshot: ScreenshotArtifact | None = None
                        if self.artifact_directory is not None:
                            _validate_screenshot_viewport(viewport)
                            screenshot_bytes = await browser_page.screenshot(
                                full_page=False,
                                animations="disabled",
                                caret="hide",
                                scale="css",
                                mask=[
                                    browser_page.locator(selector)
                                    for selector in page.screenshot_masks
                                ],
                                type="png",
                            )
                            if len(screenshot_bytes) > MAX_BROWSER_SCREENSHOT_BYTES:
                                raise BrowserFetchError("Browser screenshot exceeded size limit")
                            screenshot_width, screenshot_height = _png_dimensions(
                                screenshot_bytes
                            )
                            self.artifact_directory.mkdir(parents=True, exist_ok=True)
                            file_descriptor, temporary_name = tempfile.mkstemp(
                                dir=self.artifact_directory,
                                prefix=f"{page.id}-{viewport.id}-",
                                suffix=".png.tmp",
                            )
                            temporary_path = Path(temporary_name)
                            screenshot_path = temporary_path.with_suffix("")
                            try:
                                with os.fdopen(file_descriptor, "wb") as temporary_file:
                                    temporary_file.write(screenshot_bytes)
                                    temporary_file.flush()
                                    os.fsync(temporary_file.fileno())
                                temporary_path.replace(screenshot_path)
                            finally:
                                temporary_path.unlink(missing_ok=True)
                            screenshot = ScreenshotArtifact(
                                path=str(screenshot_path),
                                sha256=hashlib.sha256(screenshot_bytes).hexdigest(),
                                width=screenshot_width,
                                height=screenshot_height,
                            )
                        return BrowserEvidence(
                            page_id=page.id,
                            requested_url=page.url,
                            final_url=browser_page.url,
                            viewport=Viewport(
                                width=viewport.width,
                                height=viewport.height,
                                device_scale_factor=viewport.device_scale_factor,
                            ),
                            device_profile=viewport.id,
                            document_status=response.status if response is not None else None,
                            title=await browser_page.title(),
                            required_text_results=tuple(
                                TextResult(value=text, found=text in body_text)
                                for text in page.required_text
                            ),
                            required_selector_results=tuple(selector_results),
                            forbidden_text_matches=tuple(
                                text for text in page.forbidden_text if text in body_text
                            ),
                            application_failure_codes=detect_application_failure_codes(
                                body_text,
                                shortcode_names=page.application_shortcodes,
                            ),
                            console_errors=tuple(console_errors),
                            page_exceptions=tuple(page_exceptions),
                            resource_failures=tuple(resource_failures),
                            duration_ms=round((perf_counter() - started) * 1000),
                            timed_out=False,
                            screenshot=screenshot,
                        )
                    finally:
                        pending_route_tasks = tuple(route_tasks)
                        for task in pending_route_tasks:
                            task.cancel()
                        if pending_route_tasks:
                            await asyncio.gather(
                                *pending_route_tasks,
                                return_exceptions=True,
                            )
                        await context.close()
                finally:
                    await browser.close()
