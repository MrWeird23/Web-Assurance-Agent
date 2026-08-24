import fnmatch
import ipaddress
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from triage_agent import __version__
from triage_agent.manifests import CrawlManifest
from triage_agent.security import UnsafeTargetError, validate_probe_url, validate_resolved_addresses

MAX_CRAWL_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5


@dataclass(frozen=True, slots=True)
class CrawlFetchResult:
    status_code: int | None
    final_url: str
    content_type: str | None
    body: bytes
    error: str | None


@dataclass(frozen=True, slots=True)
class CrawlResult:
    url: str
    final_url: str
    depth: int
    status_code: int | None
    content_type: str | None
    error: str | None

    @property
    def healthy(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 400


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = next((value for name, value in attrs if name.lower() == "href"), None)
        if href:
            self.links.append(href)


class _MetaParser(HTMLParser):
    """Pulls the bits of a page an admin needs to guess at page config."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.has_main = False
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        elif tag == "main":
            self.has_main = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


@dataclass(frozen=True, slots=True)
class PageSuggestion:
    ok: bool
    title: str | None
    has_main: bool
    error: str | None


async def suggest_page(
    url: str,
    *,
    allowed_hosts: set[str],
    fetch: Callable[[str, set[str]], Awaitable[CrawlFetchResult]],
) -> PageSuggestion:
    """Fetch `url` once and extract config hints (title, presence of <main>)."""
    fetched = await fetch(url, allowed_hosts)
    healthy = fetched.error is None and fetched.status_code is not None and 200 <= fetched.status_code < 400
    if not healthy:
        return PageSuggestion(ok=False, title=None, has_main=False, error=fetched.error or "Unreachable")
    if not (fetched.content_type or "").lower().startswith("text/html"):
        return PageSuggestion(ok=True, title=None, has_main=False, error=None)
    parser = _MetaParser()
    parser.feed(fetched.body.decode("utf-8", errors="replace"))
    return PageSuggestion(ok=True, title=parser.title.strip() or None, has_main=parser.has_main, error=None)


def normalize_crawl_url(url: str, *, allowed_hosts: set[str]) -> str | None:
    try:
        parsed = validate_probe_url(url, allowed_hosts=allowed_hosts)
    except UnsafeTargetError:
        return None
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, query, ""))


def _excluded(url: str, patterns: tuple[str, ...]) -> bool:
    path = urlsplit(url).path or "/"
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


class SiteCrawler:
    def __init__(
        self,
        *,
        fetch: Callable[[str, set[str]], Awaitable[CrawlFetchResult]],
    ) -> None:
        self._fetch = fetch

    async def crawl(
        self,
        *,
        start_url: str,
        allowed_hosts: set[str],
        config: CrawlManifest,
    ) -> AsyncIterator[CrawlResult]:
        normalized_start = normalize_crawl_url(start_url, allowed_hosts=allowed_hosts)
        if normalized_start is None:
            return
        crawl_host = (urlsplit(normalized_start).hostname or "").lower()
        queue = deque([(normalized_start, 0)])
        queued = {normalized_start}
        checked = 0
        while queue and checked < config.max_pages:
            url, depth = queue.popleft()
            fetched = await self._fetch(url, allowed_hosts)
            checked += 1
            result = CrawlResult(
                url=url,
                final_url=fetched.final_url,
                depth=depth,
                status_code=fetched.status_code,
                content_type=fetched.content_type,
                error=fetched.error,
            )
            yield result
            if depth >= config.max_depth or not result.healthy:
                continue
            if not (fetched.content_type or "").lower().startswith("text/html"):
                continue
            parser = _LinkParser()
            parser.feed(fetched.body.decode("utf-8", errors="replace"))
            for href in parser.links:
                discovered = normalize_crawl_url(
                    urljoin(fetched.final_url, href), allowed_hosts=allowed_hosts
                )
                if (
                    discovered is None
                    or (urlsplit(discovered).hostname or "").lower() != crawl_host
                    or discovered in queued
                    or _excluded(discovered, config.exclude_paths)
                ):
                    continue
                queued.add(discovered)
                queue.append((discovered, depth + 1))


@dataclass(slots=True)
class SafeCrawlFetcher:
    client: httpx.AsyncClient
    resolver: Callable[[str], Awaitable[set[str]]]

    async def __call__(self, url: str, allowed_hosts: set[str]) -> CrawlFetchResult:
        current_url = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            try:
                parsed = validate_probe_url(current_url, allowed_hosts=allowed_hosts)
                host = parsed.hostname
                if host is None:
                    raise UnsafeTargetError("Target host is missing")
                addresses = await self.resolver(host)
                validate_resolved_addresses(addresses)
            except (OSError, UnsafeTargetError):
                return CrawlFetchResult(None, current_url, None, b"", "Unsafe target")

            response: httpx.Response | None = None
            try:
                for address in sorted(
                    addresses,
                    key=lambda value: (
                        ipaddress.ip_address(value).version,
                        int(ipaddress.ip_address(value)),
                    ),
                ):
                    request_url = httpx.URL(current_url).copy_with(host=address)
                    request = self.client.build_request(
                        "GET",
                        request_url,
                        headers={"Host": host, "User-Agent": f"Web-Assurance-Agent/{__version__}"},
                        extensions={"sni_hostname": host},
                    )
                    try:
                        response = await self.client.send(
                            request, follow_redirects=False, stream=True
                        )
                        break
                    except httpx.RequestError:
                        continue
                if response is None:
                    return CrawlFetchResult(None, current_url, None, b"", "Request failed")
                if response.is_redirect:
                    if redirect_count == MAX_REDIRECTS:
                        return CrawlFetchResult(
                            response.status_code, current_url, None, b"", "Too many redirects"
                        )
                    location = response.headers.get("location")
                    if not location:
                        return CrawlFetchResult(
                            response.status_code, current_url, None, b"", "Invalid redirect"
                        )
                    current_url = urljoin(current_url, location)
                    continue
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_CRAWL_BODY_BYTES:
                    return CrawlFetchResult(
                        response.status_code,
                        current_url,
                        response.headers.get("content-type"),
                        b"",
                        "Response too large",
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CRAWL_BODY_BYTES:
                        return CrawlFetchResult(
                            response.status_code,
                            current_url,
                            response.headers.get("content-type"),
                            b"",
                            "Response too large",
                        )
                return CrawlFetchResult(
                    response.status_code,
                    current_url,
                    response.headers.get("content-type"),
                    bytes(body),
                    None,
                )
            except (ValueError, httpx.HTTPError):
                return CrawlFetchResult(None, current_url, None, b"", "Request failed")
            finally:
                if response is not None:
                    await response.aclose()
        return CrawlFetchResult(None, current_url, None, b"", "Too many redirects")
