import io
from datetime import datetime
from pathlib import Path

from PIL import Image

from triage_agent.baselines import BaselineStore
from triage_agent.browser_checks import (
    BrowserEvidence,
    ScreenshotArtifact,
    SelectorResult,
    TextResult,
    Viewport,
)
from triage_agent.manifests import PageManifest, ViewportManifest
from triage_agent.visual_assurance import evaluate_visual_capture
from triage_agent.visual_page_checker import VisualPageChecker


def _png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (20, 20), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_missing_approved_baseline_is_pending_not_failed(tmp_path: Path) -> None:
    result = evaluate_visual_capture(
        store=BaselineStore(tmp_path),
        page_id="home",
        viewport_id="desktop",
        current_png=_png((255, 255, 255)),
        current_path="current/home-desktop.png",
        threshold_percentage=1.0,
    )

    assert result.status == "baseline_pending"
    assert result.exceeds_threshold is False
    assert result.changed_pixel_percentage is None


def test_approved_identical_baseline_passes(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    png = _png((255, 255, 255))
    store.capture(
        page_id="home",
        viewport_id="desktop",
        png_bytes=png,
        captured_at=datetime(2026, 1, 1),
    )
    store.approve(
        page_id="home",
        viewport_id="desktop",
        operator_label="operator",
        approved_at=datetime(2026, 1, 2),
    )

    result = evaluate_visual_capture(
        store=store,
        page_id="home",
        viewport_id="desktop",
        current_png=png,
        current_path="current/home-desktop.png",
        threshold_percentage=1.0,
    )

    assert result.status == "matched"
    assert result.exceeds_threshold is False
    assert result.changed_pixel_percentage == 0.0


def test_approved_changed_baseline_fails_with_bounded_evidence(tmp_path: Path) -> None:
    store = BaselineStore(tmp_path)
    store.capture(
        page_id="home",
        viewport_id="mobile",
        png_bytes=_png((255, 255, 255)),
        captured_at=datetime(2026, 1, 1),
    )
    store.approve(
        page_id="home",
        viewport_id="mobile",
        operator_label="operator",
        approved_at=datetime(2026, 1, 2),
    )

    result = evaluate_visual_capture(
        store=store,
        page_id="home",
        viewport_id="mobile",
        current_png=_png((0, 0, 0)),
        current_path="current/home-mobile.png",
        threshold_percentage=1.0,
    )

    assert result.status == "changed"
    assert result.exceeds_threshold is True
    assert result.changed_pixel_percentage == 100.0
    assert result.changed_region_count <= 50


class StubPageChecker:
    def __init__(self, evidence: BrowserEvidence) -> None:
        self.evidence = evidence

    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence:
        del page, viewport, allowed_hosts
        return self.evidence


async def test_visual_page_checker_adds_pending_evidence_without_approved_baseline(
    tmp_path: Path,
) -> None:
    screenshot_path = tmp_path / "current.png"
    screenshot_path.write_bytes(_png((255, 255, 255)))
    evidence = BrowserEvidence(
        page_id="home",
        requested_url="https://example.com/",
        final_url="https://example.com/",
        viewport=Viewport(width=20, height=20, device_scale_factor=1.0),
        page_width=20,
        page_height=20,
        device_profile="desktop",
        document_status=200,
        title="Example",
        browser_version="1.0",
        required_text_results=(TextResult(value="Example", found=True),),
        required_selector_results=(
            SelectorResult(selector="main", found=True, visible=True, width=20, height=20),
        ),
        forbidden_text_matches=(),
        application_failure_codes=(),
        plugin_assertion_results=(),
        interaction_results=(),
        console_errors=(),
        page_exceptions=(),
        resource_failures=(),
        duration_ms=10,
        timed_out=False,
        screenshot=ScreenshotArtifact(
            path=str(screenshot_path),
            sha256="a" * 64,
            width=20,
            height=20,
        ),
    )
    viewport = ViewportManifest(
        id="desktop",
        width=20,
        height=20,
        device_scale_factor=1.0,
        visual_threshold_percentage=1.0,
    )
    page = PageManifest(id="home", url="https://example.com/", viewports=(viewport,))

    result = await VisualPageChecker(
        checker=StubPageChecker(evidence),
        baseline_store=BaselineStore(tmp_path / "baselines"),
    ).run(page=page, viewport=viewport, allowed_hosts={"example.com"})

    assert result.visual_assurance is not None
    assert result.visual_assurance.status == "baseline_pending"


async def test_visual_page_checker_records_unavailable_when_artifact_cannot_be_read(
    tmp_path: Path,
) -> None:
    evidence = BrowserEvidence(
        page_id="home",
        requested_url="https://example.com/",
        final_url="https://example.com/",
        viewport=Viewport(width=20, height=20, device_scale_factor=1.0),
        page_width=20,
        page_height=20,
        device_profile="desktop",
        document_status=200,
        title="Example",
        browser_version="1.0",
        required_text_results=(),
        required_selector_results=(),
        forbidden_text_matches=(),
        application_failure_codes=(),
        plugin_assertion_results=(),
        interaction_results=(),
        console_errors=(),
        page_exceptions=(),
        resource_failures=(),
        duration_ms=10,
        timed_out=False,
        screenshot=ScreenshotArtifact(
            path=str(tmp_path / "missing.png"),
            sha256="a" * 64,
            width=20,
            height=20,
        ),
    )
    viewport = ViewportManifest(
        id="desktop",
        width=20,
        height=20,
        device_scale_factor=1.0,
        visual_threshold_percentage=1.0,
    )
    page = PageManifest(id="home", url="https://example.com/", viewports=(viewport,))

    result = await VisualPageChecker(
        checker=StubPageChecker(evidence),
        baseline_store=BaselineStore(tmp_path / "baselines"),
    ).run(page=page, viewport=viewport, allowed_hosts={"example.com"})

    assert result.visual_assurance is not None
    assert result.visual_assurance.status == "unavailable"
