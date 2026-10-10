from pathlib import Path


def test_container_installs_locked_playwright_chromium_for_non_root_runtime() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "uv run --frozen --no-dev playwright install --with-deps chromium" in dockerfile
    assert "mkdir -p /var/lib/triage/baselines /var/lib/triage/state" in dockerfile
    assert "chown -R 10001:10001 /var/lib/triage" in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_compose_mounts_writable_manifest_dir_and_artifacts() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert 'TRIAGE_SITE_MANIFEST_PATH: "/app/config/sites.yaml"' in compose
    assert 'TRIAGE_BROWSER_ARTIFACT_DIRECTORY: "/tmp/browser-artifacts"' in compose
    # The whole config dir (not just the file) is mounted read-write so the
    # admin API's atomic write (tempfile + rename) can create its temp file
    # alongside sites.yaml -- the container's rootfs stays read-only overall.
    assert "source: ./config" in compose
    assert "target: /app/config" in compose
    assert "read_only: true" in compose  # still enforced at the container level
    assert "create_host_path: false" in compose
