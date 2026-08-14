from pathlib import Path


def test_container_installs_locked_playwright_chromium_for_non_root_runtime() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "uv run --frozen --no-dev playwright install --with-deps chromium" in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_compose_mounts_read_only_manifest_and_writable_artifacts() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert 'TRIAGE_SITE_MANIFEST_PATH: "/app/config/sites.yaml"' in compose
    assert 'TRIAGE_BROWSER_ARTIFACT_DIRECTORY: "/tmp/browser-artifacts"' in compose
    assert "source: ./config/sites.yaml" in compose
    assert "target: /app/config/sites.yaml" in compose
    assert "read_only: true" in compose
    assert "create_host_path: false" in compose
