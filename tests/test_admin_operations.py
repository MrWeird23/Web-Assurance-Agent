import json
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from triage_agent.api import create_app
from triage_agent.baselines import BaselineStore
from triage_agent.browser_checks import BrowserEvidence, ScreenshotArtifact, Viewport
from triage_agent.manifest_store import ManifestStore
from triage_agent.manifests import PageManifest, ViewportManifest
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
