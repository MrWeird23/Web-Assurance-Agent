import logging

import httpx
import pytest

from triage_agent.main import create_runtime_app
from triage_agent.runtime import build_app
from triage_agent.settings import Settings


async def test_runtime_builds_healthy_dry_run_application() -> None:
    settings = Settings(
        webhook_token="a-secure-random-token",
        allowed_hosts=frozenset({"example.com"}),
        discord_webhook_url=None,
        confirmation_attempts=2,
        confirmation_delay_seconds=0,
        request_timeout_seconds=5,
    )
    app = build_app(settings)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_runtime_factory_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_WEBHOOK_TOKEN", "a-secure-random-token")
    monkeypatch.setenv("TRIAGE_ALLOWED_HOSTS", "example.com")
    app = create_runtime_app()

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
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
