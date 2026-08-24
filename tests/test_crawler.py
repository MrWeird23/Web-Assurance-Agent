from triage_agent.crawler import CrawlFetchResult, SiteCrawler
from triage_agent.manifests import CrawlManifest


async def test_crawler_discovers_same_site_links_with_bounded_depth() -> None:
    pages = {
        "https://example.com/": CrawlFetchResult(
            status_code=200,
            final_url="https://example.com/",
            content_type="text/html",
            body=b'<a href="/about">About</a><a href="https://other.test/">Other</a>',
            error=None,
        ),
        "https://example.com/about": CrawlFetchResult(
            status_code=200,
            final_url="https://example.com/about",
            content_type="text/html",
            body=b'<a href="/about/team#people">Team</a>',
            error=None,
        ),
    }

    async def fetch(url: str, allowed_hosts: set[str]) -> CrawlFetchResult:
        assert allowed_hosts == {"example.com"}
        return pages[url]

    crawler = SiteCrawler(fetch=fetch)
    results = [
        result
        async for result in crawler.crawl(
            start_url="https://example.com/",
            allowed_hosts={"example.com"},
            config=CrawlManifest(enabled=True, max_pages=10, max_depth=1),
        )
    ]

    assert [(result.url, result.depth, result.status_code) for result in results] == [
        ("https://example.com/", 0, 200),
        ("https://example.com/about", 1, 200),
    ]


async def test_crawler_deduplicates_normalized_urls_and_excludes_paths() -> None:
    pages = {
        "https://example.com/": CrawlFetchResult(
            status_code=200,
            final_url="https://example.com/",
            content_type="text/html; charset=utf-8",
            body=(
                b'<a href="/news?b=2&a=1">News</a>'
                b'<a href="/news?a=1&b=2#latest">Duplicate</a>'
                b'<a href="/wp-admin/">Admin</a>'
                b'<a href="mailto:test@example.com">Mail</a>'
            ),
            error=None,
        ),
        "https://example.com/news?a=1&b=2": CrawlFetchResult(
            status_code=404,
            final_url="https://example.com/news?a=1&b=2",
            content_type="text/html",
            body=b"missing",
            error=None,
        ),
    }

    async def fetch(url: str, allowed_hosts: set[str]) -> CrawlFetchResult:
        return pages[url]

    results = [
        result
        async for result in SiteCrawler(fetch=fetch).crawl(
            start_url="https://example.com/",
            allowed_hosts={"example.com"},
            config=CrawlManifest(
                enabled=True,
                max_pages=10,
                max_depth=2,
                exclude_paths=("/wp-admin/*",),
            ),
        )
    ]

    assert [result.url for result in results] == [
        "https://example.com/",
        "https://example.com/news?a=1&b=2",
    ]
    assert results[-1].healthy is False


async def test_crawler_stops_at_maximum_page_count() -> None:
    async def fetch(url: str, allowed_hosts: set[str]) -> CrawlFetchResult:
        body = b'<a href="/one">One</a><a href="/two">Two</a>'
        return CrawlFetchResult(200, url, "text/html", body, None)

    results = [
        result
        async for result in SiteCrawler(fetch=fetch).crawl(
            start_url="https://example.com/",
            allowed_hosts={"example.com"},
            config=CrawlManifest(enabled=True, max_pages=2, max_depth=2),
        )
    ]

    assert len(results) == 2
