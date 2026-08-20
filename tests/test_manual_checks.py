import asyncio
from dataclasses import replace

import httpx

from triage_agent.api import create_app
from triage_agent.browser_checks import (
    BrowserEvidence,
    PluginAssertionResult,
    ScreenshotArtifact,
    SelectorResult,
    TextResult,
    Viewport,
)
from triage_agent.manifests import PageManifest, ViewportManifest, parse_site_manifest


class StubEngine:
    async def handle(self, payload: dict[str, object]) -> None:
        del payload


MANIFEST = """
version: 1
sites:
  - id: example
    allowed_hosts: [example.com]
    pages:
      - id: home
        url: https://example.com/
        viewports:
          - {id: desktop, width: 1440, height: 900, device_scale_factor: 1.0}
          - {id: mobile, width: 390, height: 844, device_scale_factor: 2.0}
        required_text: [Welcome]
        required_selectors: [main]
"""


class StubPageChecker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, frozenset[str]]] = []

    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence:
        page_id = page.id
        viewport_id = viewport.id
        self.calls.append((page_id, viewport_id, frozenset(allowed_hosts)))
        return BrowserEvidence(
            page_id=page_id,
            requested_url=page.url,
            final_url=page.url,
            viewport=Viewport(
                width=viewport.width,
                height=viewport.height,
                device_scale_factor=viewport.device_scale_factor,
            ),
            page_width=viewport.width,
            page_height=viewport.height,
            device_profile=viewport_id,
            document_status=200,
            title="Example",
            browser_version="120.0.6099.109",
            required_text_results=(TextResult(value="Welcome", found=True),),
            required_selector_results=(
                SelectorResult(selector="main", found=True, visible=True, width=100, height=100),
            ),
            forbidden_text_matches=(),
            application_failure_codes=(),
            plugin_assertion_results=(),
            interaction_results=(),
            console_errors=(),
            page_exceptions=(),
            resource_failures=(),
            duration_ms=12,
            timed_out=False,
            screenshot=ScreenshotArtifact(
                path="home/desktop.png",
                sha256="a" * 64,
                width=1440,
                height=900,
            ),
        )


class BlockingPageChecker(StubPageChecker):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence:
        self.started.set()
        await self.release.wait()
        return await super().run(
            page=page,
            viewport=viewport,
            allowed_hosts=allowed_hosts,
        )


class PluginAssertionFailingPageChecker(StubPageChecker):
    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence:
        evidence = await super().run(page=page, viewport=viewport, allowed_hosts=allowed_hosts)
        return replace(
            evidence,
            plugin_assertion_results=(
                PluginAssertionResult(
                    assertion_id="contact-form",
                    kind="contact-form-7",
                    satisfied=False,
                ),
            ),
        )


async def test_manual_check_reports_failed_plugin_assertion_ids_without_selectors() -> None:
    checker = PluginAssertionFailingPageChecker()
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_registry=parse_site_manifest(MANIFEST),
        page_checker=checker,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/checks/pages/home", headers={"X-Triage-Token": "expected-secret"}
        )

    payload = response.json()
    assert payload["classification"] == "failed"
    assert payload["evidence"]["failure_codes"] == ["plugin_assertion_failed"]
    assert payload["evidence"]["failed_plugin_assertions"] == ["contact-form"]
    assert "wpcf7" not in str(payload)
    assert "selector" not in str(payload).lower()


async def test_manual_check_requires_same_authentication_boundary() -> None:
    checker = StubPageChecker()
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_registry=parse_site_manifest(MANIFEST),
        page_checker=checker,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/checks/pages/home")

    assert response.status_code == 401
    assert checker.calls == []


async def test_unknown_page_returns_not_found_without_browser_startup() -> None:
    checker = StubPageChecker()
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_registry=parse_site_manifest(MANIFEST),
        page_checker=checker,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/checks/pages/missing", headers={"X-Triage-Token": "expected-secret"}
        )

    assert response.status_code == 404
    assert checker.calls == []


async def test_manual_check_runs_only_manifest_page_and_returns_concise_evidence() -> None:
    checker = StubPageChecker()
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_registry=parse_site_manifest(MANIFEST),
        page_checker=checker,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/checks/pages/home", headers={"X-Triage-Token": "expected-secret"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["check_id"]) == 32
    assert payload["page_id"] == "home"
    assert payload["classification"] == "healthy"
    assert payload["evidence"] == {
        "viewports_checked": 2,
        "failed_viewports": [],
        "failure_codes": [],
        "failed_plugin_assertions": [],
    }
    assert payload["artifacts"] == ["home/desktop.png", "home/desktop.png"]
    assert checker.calls == [
        ("home", "desktop", frozenset({"example.com"})),
        ("home", "mobile", frozenset({"example.com"})),
    ]
    assert "url" not in str(payload).lower()


async def test_manual_check_endpoint_is_not_registered_without_browser_integration() -> None:
    app = create_app(engine=StubEngine(), webhook_token="expected-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/checks/pages/home", headers={"X-Triage-Token": "expected-secret"}
        )

    assert response.status_code == 404


async def test_manual_check_rejects_concurrent_request_when_capacity_is_exhausted() -> None:
    checker = BlockingPageChecker()
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_registry=parse_site_manifest(MANIFEST),
        page_checker=checker,
        manual_check_concurrency=1,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = asyncio.create_task(
            client.post(
                "/checks/pages/home",
                headers={"X-Triage-Token": "expected-secret"},
            )
        )
        await checker.started.wait()
        second = await client.post(
            "/checks/pages/home",
            headers={"X-Triage-Token": "expected-secret"},
        )
        checker.release.set()
        first_response = await first

    assert second.status_code == 429
    assert second.headers["Retry-After"] == "1"
    assert first_response.status_code == 200
