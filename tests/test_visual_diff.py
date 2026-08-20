import io

import pytest
from PIL import Image

from triage_agent.visual_diff import DiffRegion, compare_screenshots


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_with_patch(
    width: int,
    height: int,
    base: tuple[int, int, int],
    patch: tuple[int, int, int, int, int, int, int],
) -> bytes:
    x, y, patch_width, patch_height, r, g, b = patch
    image = Image.new("RGB", (width, height), base)
    for py in range(y, y + patch_height):
        for px in range(x, x + patch_width):
            image.putpixel((px, py), (r, g, b))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_identical_screenshots_have_zero_difference() -> None:
    baseline = _png(100, 100, (255, 255, 255))
    current = _png(100, 100, (255, 255, 255))

    result = compare_screenshots(
        page_id="home",
        viewport_id="desktop",
        baseline_png=baseline,
        current_png=current,
        baseline_path="baselines/home-desktop.png",
        current_path="current/home-desktop.png",
        threshold_percentage=1.0,
    )

    assert result.perceptual_difference == 0.0
    assert result.changed_pixel_percentage == 0.0
    assert result.changed_regions == ()
    assert result.exceeds_threshold is False
    assert result.page_id == "home"
    assert result.viewport_id == "desktop"
    assert result.baseline_path == "baselines/home-desktop.png"
    assert result.current_path == "current/home-desktop.png"


def test_localized_change_is_detected_and_bounded() -> None:
    baseline = _png(100, 100, (255, 255, 255))
    current = _png_with_patch(100, 100, (255, 255, 255), (10, 10, 20, 20, 255, 0, 0))

    result = compare_screenshots(
        page_id="home",
        viewport_id="desktop",
        baseline_png=baseline,
        current_png=current,
        baseline_path="baselines/home-desktop.png",
        current_path="current/home-desktop.png",
        threshold_percentage=1.0,
    )

    assert result.changed_pixel_percentage == pytest.approx(4.0, abs=0.01)
    assert result.perceptual_difference > 0.0
    assert result.exceeds_threshold is True
    assert len(result.changed_regions) == 1
    region = result.changed_regions[0]
    assert region.x <= 10
    assert region.y <= 10
    assert region.x + region.width >= 30
    assert region.y + region.height >= 30


def test_change_below_threshold_does_not_exceed_it() -> None:
    baseline = _png(100, 100, (255, 255, 255))
    current = _png_with_patch(100, 100, (255, 255, 255), (10, 10, 20, 20, 255, 0, 0))

    result = compare_screenshots(
        page_id="home",
        viewport_id="desktop",
        baseline_png=baseline,
        current_png=current,
        baseline_path="baselines/home-desktop.png",
        current_path="current/home-desktop.png",
        threshold_percentage=10.0,
    )

    assert result.exceeds_threshold is False


def test_subtle_uniform_difference_scores_but_stays_below_pixel_tolerance() -> None:
    baseline = _png(50, 50, (200, 200, 200))
    current = _png(50, 50, (200, 200, 100))

    result = compare_screenshots(
        page_id="home",
        viewport_id="desktop",
        baseline_png=baseline,
        current_png=current,
        baseline_path="baselines/home-desktop.png",
        current_path="current/home-desktop.png",
        threshold_percentage=1.0,
    )

    assert result.perceptual_difference > 0.0
    assert result.changed_pixel_percentage == 0.0
    assert result.changed_regions == ()
    assert result.exceeds_threshold is False


def test_two_disjoint_changes_produce_two_regions() -> None:
    image = Image.new("RGB", (200, 200), (255, 255, 255))
    for x, y in [(5, 5), (150, 150)]:
        for py in range(y, y + 10):
            for px in range(x, x + 10):
                image.putpixel((px, py), (0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    current = buffer.getvalue()
    baseline = _png(200, 200, (255, 255, 255))

    result = compare_screenshots(
        page_id="home",
        viewport_id="desktop",
        baseline_png=baseline,
        current_png=current,
        baseline_path="baselines/home-desktop.png",
        current_path="current/home-desktop.png",
        threshold_percentage=1.0,
        tile_size=16,
    )

    assert len(result.changed_regions) == 2


def test_dimension_mismatch_is_treated_as_maximal_difference() -> None:
    baseline = _png(100, 100, (255, 255, 255))
    current = _png(120, 100, (255, 255, 255))

    result = compare_screenshots(
        page_id="home",
        viewport_id="desktop",
        baseline_png=baseline,
        current_png=current,
        baseline_path="baselines/home-desktop.png",
        current_path="current/home-desktop.png",
        threshold_percentage=50.0,
    )

    assert result.perceptual_difference == 1.0
    assert result.changed_pixel_percentage == 100.0
    assert result.exceeds_threshold is True
    assert result.changed_regions == (DiffRegion(x=0, y=0, width=120, height=100),)


def test_diff_image_is_a_valid_png_matching_current_dimensions() -> None:
    baseline = _png(64, 48, (255, 255, 255))
    current = _png_with_patch(64, 48, (255, 255, 255), (0, 0, 8, 8, 0, 0, 0))

    result = compare_screenshots(
        page_id="home",
        viewport_id="desktop",
        baseline_png=baseline,
        current_png=current,
        baseline_path="baselines/home-desktop.png",
        current_path="current/home-desktop.png",
        threshold_percentage=1.0,
    )

    diff_image = Image.open(io.BytesIO(result.diff_image))
    assert diff_image.format == "PNG"
    assert diff_image.size == (64, 48)


@pytest.mark.parametrize("pixel_tolerance", [-1, 255])
def test_rejects_out_of_range_pixel_tolerance(pixel_tolerance: int) -> None:
    baseline = _png(10, 10, (255, 255, 255))
    current = _png(10, 10, (255, 255, 255))

    with pytest.raises(ValueError, match="pixel_tolerance"):
        compare_screenshots(
            page_id="home",
            viewport_id="desktop",
            baseline_png=baseline,
            current_png=current,
            baseline_path="baselines/home-desktop.png",
            current_path="current/home-desktop.png",
            threshold_percentage=1.0,
            pixel_tolerance=pixel_tolerance,
        )


@pytest.mark.parametrize("threshold_percentage", [-1.0, 100.1])
def test_rejects_out_of_range_threshold_percentage(threshold_percentage: float) -> None:
    baseline = _png(10, 10, (255, 255, 255))
    current = _png(10, 10, (255, 255, 255))

    with pytest.raises(ValueError, match="threshold_percentage"):
        compare_screenshots(
            page_id="home",
            viewport_id="desktop",
            baseline_png=baseline,
            current_png=current,
            baseline_path="baselines/home-desktop.png",
            current_path="current/home-desktop.png",
            threshold_percentage=threshold_percentage,
        )


def test_rejects_screenshot_exceeding_the_pixel_budget() -> None:
    huge = _png(4200, 4000, (255, 255, 255))
    small = _png(10, 10, (255, 255, 255))

    with pytest.raises(ValueError, match="pixel limit"):
        compare_screenshots(
            page_id="home",
            viewport_id="desktop",
            baseline_png=huge,
            current_png=small,
            baseline_path="baselines/home-desktop.png",
            current_path="current/home-desktop.png",
            threshold_percentage=1.0,
        )
