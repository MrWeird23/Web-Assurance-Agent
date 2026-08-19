"""End-to-end evaluation of current screenshots against approved baselines."""

from triage_agent.baselines import BaselineStore
from triage_agent.browser_checks import VisualAssuranceResult
from triage_agent.visual_diff import compare_screenshots


def evaluate_visual_capture(
    *,
    store: BaselineStore,
    page_id: str,
    viewport_id: str,
    current_png: bytes,
    current_path: str,
    threshold_percentage: float,
) -> VisualAssuranceResult:
    """Compare a capture only when a human-approved baseline exists."""
    baseline = store.current(page_id=page_id, viewport_id=viewport_id)
    if baseline is None:
        return VisualAssuranceResult(
            status="baseline_pending",
            exceeds_threshold=False,
            changed_pixel_percentage=None,
            changed_region_count=0,
        )

    diff = compare_screenshots(
        page_id=page_id,
        viewport_id=viewport_id,
        baseline_png=store.load_approved_png(page_id=page_id, viewport_id=viewport_id),
        current_png=current_png,
        baseline_path=f"approved/{page_id}-{viewport_id}.png",
        current_path=current_path,
        threshold_percentage=threshold_percentage,
    )
    return VisualAssuranceResult(
        status="changed" if diff.exceeds_threshold else "matched",
        exceeds_threshold=diff.exceeds_threshold,
        changed_pixel_percentage=diff.changed_pixel_percentage,
        changed_region_count=len(diff.changed_regions),
    )
