import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from triage_agent.api import create_app
from triage_agent.baselines import BaselineStore
from triage_agent.browser_checks import BrowserEvidence, ScreenshotArtifact, Viewport
from triage_agent.crawler import CrawlResult
from triage_agent.manifest_store import ManifestStore
from triage_agent.manifests import CrawlManifest, PageManifest, ViewportManifest
from triage_agent.probes import ProbeResult

MANIFEST = """\
version: 1
sites:
  - id: example
    allowed_hosts: [example.com]
    pages:
      - id: home
        url: https://example.com/
        viewports:
          - {id: desktop, width: 1440, height: 900, device_scale_factor: 1.0}
"""


class StubEngine:
    async def handle(self, payload: dict[str, object]) -> None:
        del payload


class StubPageChecker:
    console_errors: tuple[str, ...] = ()

    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence:
        del allowed_hosts
        return BrowserEvidence(
            page_id=page.id,
            requested_url=page.url,
            final_url=page.url,
            viewport=Viewport(viewport.width, viewport.height, viewport.device_scale_factor),
            page_width=viewport.width,
            page_height=viewport.height,
            device_profile=viewport.id,
            document_status=200,
            title="Example",
            browser_version="test",
            required_text_results=(),
            required_selector_results=(),
            forbidden_text_matches=(),
            application_failure_codes=(),
            plugin_assertion_results=(),
            interaction_results=(),
            console_errors=self.console_errors,
            page_exceptions=(),
            resource_failures=(),
            duration_ms=12,
            timed_out=False,
            screenshot=ScreenshotArtifact("example.png", "a" * 64, viewport.width, viewport.height),
        )


class StubSiteCrawler:
    async def crawl(
        self,
        *,
        start_url: str,
        allowed_hosts: set[str],
        config: CrawlManifest,
    ) -> AsyncIterator[CrawlResult]:
        assert allowed_hosts == {"example.com"}
        assert config.enabled is True
        yield CrawlResult(start_url, start_url, 0, 200, "text/html", None)
        yield CrawlResult(
            "https://example.com/missing",
            "https://example.com/missing",
            1,
            404,
            "text/html",
            None,
        )


def _store(tmp_path: Path) -> ManifestStore:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    return ManifestStore(path, permitted_hosts={"example.com"})


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    return output.getvalue()


async def test_admin_can_test_a_configured_link(tmp_path: Path) -> None:
    async def probe(url: str, allowed_hosts: set[str]) -> ProbeResult:
        assert url == "https://example.com/"
        assert allowed_hosts == {"example.com"}
        return ProbeResult(True, 200, 18, url, None, "example")

    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=_store(tmp_path),
        admin_probe=probe,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/admin/api/pages/home/test-link",
            headers={"Authorization": "Bearer expected-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status_code": 200,
        "latency_ms": 18,
        "final_url": "https://example.com/",
        "error": None,
    }


async def test_admin_runs_page_tests_as_streamed_ndjson(tmp_path: Path) -> None:
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=_store(tmp_path),
        page_checker=StubPageChecker(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/admin/api/pages/home/run-tests",
            headers={"Authorization": "Bearer expected-secret"},
        )

    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert [event["event"] for event in events] == ["start", "viewport", "result"]
    assert events[1] == {
        "event": "viewport",
        "viewport_id": "desktop",
        "status": "passed",
        "duration_ms": 12,
        "failure_codes": [],
        "information_codes": [],
        "console_errors": [],
    }
    assert events[-1]["classification"] == "healthy"


async def test_admin_runs_full_triage_for_every_configured_page(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = store.read()
    second_page = deepcopy(payload["sites"][0]["pages"][0])
    second_page.update({"id": "contact", "url": "https://example.com/contact"})
    payload["sites"][0]["pages"].append(second_page)
    store.replace(payload)
    checker = StubPageChecker()
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=store,
        page_checker=checker,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/admin/api/run-full-triage",
            headers={"Authorization": "Bearer expected-secret"},
        )

    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert [event["event"] for event in events] == [
        "triage_start",
        "page_start",
        "viewport",
        "page_result",
        "page_start",
        "viewport",
        "page_result",
        "triage_result",
    ]
    assert [event["page_id"] for event in events if event["event"] == "page_start"] == [
        "home",
        "contact",
    ]
    assert events[-1] == {
        "event": "triage_result",
        "classification": "healthy",
        "pages_total": 2,
        "pages_healthy": 2,
        "pages_failed": 0,
    }


async def test_admin_full_triage_streams_enabled_site_crawl(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = store.read()
    payload["sites"][0]["pages"][0]["crawl"] = {
        "enabled": True,
        "max_pages": 25,
        "max_depth": 2,
    }
    store.replace(payload)
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=store,
        page_checker=StubPageChecker(),
        site_crawler=StubSiteCrawler(),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/admin/api/run-full-triage",
            headers={"Authorization": "Bearer expected-secret"},
        )

    events = [json.loads(line) for line in response.text.splitlines()]
    crawl_events = [event for event in events if event["event"].startswith("crawl_")]
    assert crawl_events == [
        {"event": "crawl_start", "page_id": "home", "max_pages": 25, "max_depth": 2},
        {
            "event": "crawl_page",
            "page_id": "home",
            "url": "https://example.com/",
            "final_url": "https://example.com/",
            "depth": 0,
            "status_code": 200,
            "healthy": True,
            "error": None,
        },
        {
            "event": "crawl_page",
            "page_id": "home",
            "url": "https://example.com/missing",
            "final_url": "https://example.com/missing",
            "depth": 1,
            "status_code": 404,
            "healthy": False,
            "error": None,
        },
        {
            "event": "crawl_result",
            "page_id": "home",
            "pages_checked": 2,
            "pages_failed": 1,
            "classification": "failed",
        },
    ]
    assert events[-1]["classification"] == "failed"


async def test_admin_full_triage_requires_authentication(tmp_path: Path) -> None:
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=_store(tmp_path),
        page_checker=StubPageChecker(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/admin/api/run-full-triage")

    assert response.status_code == 401


async def test_admin_test_run_honours_ignored_console_errors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = store.read()
    payload["sites"][0]["pages"][0]["ignore_console_errors"] = True
    store.replace(payload)
    checker = StubPageChecker()
    checker.console_errors = ("Known third-party warning", "Known third-party warning")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=store,
        page_checker=checker,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/api/pages/home/run-tests",
            headers={"Authorization": "Bearer expected-secret"},
        )

    events = [json.loads(line) for line in response.text.splitlines()]
    viewport = next(event for event in events if event["event"] == "viewport")
    assert viewport["status"] == "passed"
    assert viewport["failure_codes"] == []
    assert viewport["information_codes"] == ["console_error"]
    assert viewport["console_errors"] == ["Known third-party warning"]


async def test_admin_uploads_and_approves_reference_screenshot(tmp_path: Path) -> None:
    baselines = BaselineStore(tmp_path / "baselines")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=_store(tmp_path),
        baseline_store=baselines,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.put(
            "/admin/api/pages/home/baselines/desktop",
            headers={
                "Authorization": "Bearer expected-secret",
                "Content-Type": "image/png",
                "X-Operator-Label": "Rodrigo",
            },
            content=_png(),
        )

    approved = baselines.current(page_id="home", viewport_id="desktop")
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert approved is not None
    assert approved.operator_label == "Rodrigo"


async def test_admin_captures_previews_and_approves_reference_screenshot(
    tmp_path: Path,
) -> None:
    screenshot = tmp_path / "captured.png"
    screenshot.write_bytes(_png())

    class CapturePageChecker(StubPageChecker):
        async def run(
            self,
            *,
            page: PageManifest,
            viewport: ViewportManifest,
            allowed_hosts: set[str],
        ) -> BrowserEvidence:
            evidence = await super().run(
                page=page, viewport=viewport, allowed_hosts=allowed_hosts
            )
            return replace(
                evidence,
                screenshot=ScreenshotArtifact(
                    path=str(screenshot),
                    sha256="unused",
                    width=20,
                    height=20,
                ),
            )

    baselines = BaselineStore(tmp_path / "baselines")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=_store(tmp_path),
        baseline_store=baselines,
        page_checker=CapturePageChecker(),
    )
    headers = {"Authorization": "Bearer expected-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        captured = await client.post(
            "/admin/api/pages/home/baselines/desktop/capture", headers=headers
        )
        preview = await client.get(
            "/admin/api/pages/home/baselines/desktop/pending", headers=headers
        )
        approved_response = await client.post(
            "/admin/api/pages/home/baselines/desktop/approve",
            headers={**headers, "X-Operator-Label": "Rodrigo"},
        )

    approved = baselines.current(page_id="home", viewport_id="desktop")
    assert captured.status_code == 200
    assert captured.json()["status"] == "pending"
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.content == _png()
    assert approved_response.status_code == 200
    assert approved_response.json()["status"] == "approved"
    assert approved is not None
    assert approved.operator_label == "Rodrigo"
