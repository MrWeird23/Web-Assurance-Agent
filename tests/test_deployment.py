from pathlib import Path


def test_container_installs_locked_playwright_chromium_for_non_root_runtime() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "uv run --frozen --no-dev playwright install --with-deps chromium" in dockerfile
    assert "USER 10001:10001" in dockerfile
