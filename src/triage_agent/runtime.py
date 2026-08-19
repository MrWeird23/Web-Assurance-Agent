import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from triage_agent.api import PageChecker, create_app
from triage_agent.baselines import BaselineStore
from triage_agent.browser_runner import PlaywrightBrowserRunner
from triage_agent.discord import DiscordPublisher
from triage_agent.engine import Publisher, TriageEngine
from triage_agent.manifests import load_site_manifest
from triage_agent.probes import ProbeResult, probe_url, resolve_addresses
from triage_agent.settings import Settings
from triage_agent.visual_page_checker import VisualPageChecker

logger = logging.getLogger(__name__)


def build_app(settings: Settings) -> FastAPI:
    client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    async def probe(url: str) -> ProbeResult:
        return await probe_url(
            url,
            allowed_hosts=set(settings.allowed_hosts),
            client=client,
            resolver=resolve_addresses,
        )

    publisher: Publisher
    if settings.discord_webhook_url:
        publisher = DiscordPublisher(webhook_url=settings.discord_webhook_url, client=client)
    else:

        async def dry_run_publish(payload: dict[str, Any]) -> None:
            logger.info("dry_run_discord_payload=%s", json.dumps(payload, sort_keys=True))

        publisher = dry_run_publish

    engine = TriageEngine(
        probe=probe,
        publish=publisher,
        confirmation_attempts=settings.confirmation_attempts,
        confirmation_delay_seconds=settings.confirmation_delay_seconds,
    )

    manifest_registry = None
    page_checker: PageChecker | None = None
    if settings.site_manifest_path is not None:
        manifest_registry = load_site_manifest(settings.site_manifest_path)
        browser_checker = PlaywrightBrowserRunner(
            resolver=resolve_addresses,
            artifact_directory=settings.browser_artifact_directory,
        )
        if settings.visual_baseline_directory is not None:
            page_checker = VisualPageChecker(
                checker=browser_checker,
                baseline_store=BaselineStore(settings.visual_baseline_directory),
            )
        else:
            page_checker = browser_checker

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.aclose()

    return create_app(
        engine=engine,
        webhook_token=settings.webhook_token,
        lifespan=lifespan,
        manifest_registry=manifest_registry,
        page_checker=page_checker,
        manual_check_concurrency=settings.manual_check_concurrency,
    )
