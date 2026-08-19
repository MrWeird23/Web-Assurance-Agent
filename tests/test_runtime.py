import logging
from pathlib import Path

import httpx
import pytest

from triage_agent.main import create_runtime_app
from triage_agent.runtime import build_app
from triage_agent.settings import Settings

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
"""


async def test_runtime_builds_healthy_dry_run_application() -> None:
    settings = Settings(
        webhook_token="a-secure-random-token",
        allowed_hosts=frozenset({"example.com"}),
        discord_webhook_url=None,
        confirmation_attempts=2,
        confirmation_delay_seconds=0,
        request_timeout_seconds=5,
        site_manifest_path=None,
        browser_artifact_directory=None,
        visual_baseline_directory=None,
    )
    app = build_app(settings)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_runtime_registers_manual_check_when_manifest_is_configured(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "sites.yaml"
    manifest_path.write_text(MANIFEST, encoding="utf-8")
    settings = Settings(
        webhook_token="a-secure-random-token",
        allowed_hosts=frozenset({"example.com"}),
        discord_webhook_url=None,
        confirmation_attempts=2,
        confirmation_delay_seconds=0,
        request_timeout_seconds=5,
        site_manifest_path=manifest_path,
        browser_artifact_directory=tmp_path / "artifacts",
        visual_baseline_directory=tmp_path / "baselines",
    )

    app = build_app(settings)
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/checks/pages/{page_id}" in paths


async def test_runtime_factory_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_WEBHOOK_TOKEN", "a-secure-random-token")
    monkeypatch.setenv("TRIAGE_ALLOWED_HOSTS", "example.com")
    app = create_runtime_app()

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        response = await client.get("/healthz")

    assert response.status_code == 200


def test_runtime_suppresses_http_client_request_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_WEBHOOK_TOKEN", "a-secure-random-token")
    monkeypatch.setenv("TRIAGE_ALLOWED_HOSTS", "example.com")
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    logging.getLogger("httpcore").setLevel(logging.NOTSET)

    create_runtime_app()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
