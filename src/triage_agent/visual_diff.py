"""Perceptual screenshot comparison (Milestone 3.2 — Visual regression).

Policy (see ROADMAP.md §3.2):
- never rely on exact full-page pixel equality;
- thresholds are versioned per page and viewport (the caller supplies
  ``threshold_percentage``; this module has no opinion on where it comes from);
- a visual difference is evidence, not an automatic outage — callers decide
  what ``exceeds_threshold`` means for classification;
- this module never inspects deterministic browser evidence and never runs AI
  vision — it is a pure, bounded image comparison.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw

# ponytail: same screenshot pixel budget the browser runner already enforces
# when capturing; re-checked here since this module is also callable directly.
_MAX_DIFF_IMAGE_PIXELS = 16_777_216
_MAX_DIFF_REGIONS = 50

DEFAULT_PIXEL_TOLERANCE = 24  # per-pixel luma delta (0-255) ignored as anti-aliasing noise
DEFAULT_TILE_SIZE = 32


@dataclass(frozen=True, slots=True)
class DiffRegion:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class VisualDiffResult:
    page_id: str
    viewport_id: str
    baseline_path: str
    current_path: str
    perceptual_difference: float
    changed_pixel_percentage: float
    changed_regions: tuple[DiffRegion, ...]
    exceeds_threshold: bool
    diff_image: bytes


def compare_screenshots(
    *,
    page_id: str,
    viewport_id: str,
    baseline_png: bytes,
    current_png: bytes,
    baseline_path: str,
    current_path: str,
    threshold_percentage: float,
    pixel_tolerance: int = DEFAULT_PIXEL_TOLERANCE,
    tile_size: int = DEFAULT_TILE_SIZE,
) -> VisualDiffResult:
    if not 0 <= pixel_tolerance <= 254:
        raise ValueError("pixel_tolerance must be between 0 and 254")
    if not 0.0 <= threshold_percentage <= 100.0:
        raise ValueError("threshold_percentage must be between 0 and 100")

    baseline = _decode(baseline_png)
    current = _decode(current_png)

    if baseline.size != current.size:
        width, height = current.size
        whole_page = (DiffRegion(x=0, y=0, width=width, height=height),)
        return VisualDiffResult(
            page_id=page_id,
            viewport_id=viewport_id,
            baseline_path=baseline_path,
            current_path=current_path,
            perceptual_difference=1.0,
            changed_pixel_percentage=100.0,
            changed_regions=whole_page,
            exceeds_threshold=True,
            diff_image=_render_diff_image(current, whole_page),
        )

    width, height = current.size
    total_pixels = width * height

    diff = ImageChops.difference(baseline, current).convert("L")
    histogram = diff.histogram()
    changed_pixels = sum(histogram[pixel_tolerance + 1 :])
    changed_pixel_percentage = (changed_pixels / total_pixels * 100.0) if total_pixels else 0.0
    perceptual_difference = (
        sum(level * count for level, count in enumerate(histogram)) / (255.0 * total_pixels)
        if total_pixels
        else 0.0
    )

    mask = diff.point(lambda level: 255 if level > pixel_tolerance else 0)
    regions = _changed_regions(mask, tile_size=tile_size)

    return VisualDiffResult(
        page_id=page_id,
        viewport_id=viewport_id,
        baseline_path=baseline_path,
        current_path=current_path,
        perceptual_difference=perceptual_difference,
        changed_pixel_percentage=changed_pixel_percentage,
        changed_regions=regions,
        exceeds_threshold=changed_pixel_percentage > threshold_percentage,
        diff_image=_render_diff_image(current, regions),
    )


def _decode(png_bytes: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png_bytes))
    image.load()
    if image.size[0] * image.size[1] > _MAX_DIFF_IMAGE_PIXELS:
        raise ValueError("Screenshot exceeded the visual diff pixel limit")
    return image.convert("RGB")


def _changed_regions(mask: Image.Image, *, tile_size: int) -> tuple[DiffRegion, ...]:
    width, height = mask.size
    cols = -(-width // tile_size)
    rows = -(-height // tile_size)

    flagged: set[tuple[int, int]] = set()
    for row in range(rows):
        for col in range(cols):
            box = (
                col * tile_size,
                row * tile_size,
                min((col + 1) * tile_size, width),
                min((row + 1) * tile_size, height),
            )
            if mask.crop(box).getbbox() is not None:
                flagged.add((col, row))

    regions = [
        _bounding_region(component, tile_size=tile_size, width=width, height=height)
        for component in _connected_components(flagged)
    ]
    # ponytail: bounded to the MAX_DIFF_REGIONS largest components; a page with
    # more disjoint changes than that gets summarized, not silently truncated
    # to a wrong count — callers see len() == cap and can treat it as "many".
    regions.sort(key=lambda region: region.width * region.height, reverse=True)
    return tuple(regions[:_MAX_DIFF_REGIONS])


def _bounding_region(
    component: set[tuple[int, int]], *, tile_size: int, width: int, height: int
) -> DiffRegion:
    min_col = min(col for col, _ in component)
    min_row = min(row for _, row in component)
    max_col = max(col for col, _ in component)
    max_row = max(row for _, row in component)
    x = min_col * tile_size
    y = min_row * tile_size
    return DiffRegion(
        x=x,
        y=y,
        width=min((max_col + 1) * tile_size, width) - x,
        height=min((max_row + 1) * tile_size, height) - y,
    )


def _connected_components(cells: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    remaining = set(cells)
    components: list[set[tuple[int, int]]] = []
    while remaining:
        start = next(iter(remaining))
        stack = [start]
        component: set[tuple[int, int]] = set()
        while stack:
            cell = stack.pop()
            if cell in component:
                continue
            component.add(cell)
            remaining.discard(cell)
            x, y = cell
            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in remaining:
                    stack.append(neighbor)
        components.append(component)
    return components


def _render_diff_image(current: Image.Image, regions: tuple[DiffRegion, ...]) -> bytes:
    highlighted = current.copy()
    draw = ImageDraw.Draw(highlighted)
    for region in regions:
        draw.rectangle(
            (region.x, region.y, region.x + region.width - 1, region.y + region.height - 1),
            outline=(255, 0, 0),
            width=3,
        )
    buffer = io.BytesIO()
    highlighted.save(buffer, format="PNG")
    return buffer.getvalue()
