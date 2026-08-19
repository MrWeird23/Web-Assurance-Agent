"""Page-checker decorator that adds approved-baseline visual evidence."""

from dataclasses import replace
from pathlib import Path
from typing import Protocol

from triage_agent.baselines import BaselineStore
from triage_agent.browser_checks import BrowserEvidence, VisualAssuranceResult
from triage_agent.manifests import PageManifest, ViewportManifest
from triage_agent.visual_assurance import evaluate_visual_capture


class PageChecker(Protocol):
    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence: ...


class VisualPageChecker:
    def __init__(self, *, checker: PageChecker, baseline_store: BaselineStore) -> None:
        self._checker = checker
        self._baseline_store = baseline_store

    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence:
        evidence = await self._checker.run(
            page=page,
            viewport=viewport,
            allowed_hosts=allowed_hosts,
        )
        threshold = viewport.visual_threshold_percentage
        if threshold is None or evidence.screenshot is None:
            return evidence

        screenshot_path = Path(evidence.screenshot.path)
        try:
            visual = evaluate_visual_capture(
                store=self._baseline_store,
                page_id=page.id,
                viewport_id=viewport.id,
                current_png=screenshot_path.read_bytes(),
                current_path=str(screenshot_path),
                threshold_percentage=threshold,
            )
        except (OSError, ValueError):
            visual = VisualAssuranceResult(
                status="unavailable",
                exceeds_threshold=False,
                changed_pixel_percentage=None,
                changed_region_count=0,
            )
        return replace(evidence, visual_assurance=visual)
