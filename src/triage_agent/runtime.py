import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from triage_agent.api import PageChecker, WordPressHealthChecker, create_app, run_page_check
from triage_agent.baselines import BaselineStore
from triage_agent.browser_runner import PlaywrightBrowserRunner
from triage_agent.classification import Incident, IncidentKind
from triage_agent.discord import DiscordPublisher, DiscordPublishError
from triage_agent.durable_registry import DurableIncidentRegistry
from triage_agent.engine import Publisher, TriageEngine
from triage_agent.events import EventState, KumaEvent
from triage_agent.manifests import ManifestRegistry, PageManifest, load_site_manifest
from triage_agent.probes import ProbeResult, probe_url, resolve_addresses
from triage_agent.reporting import render_browser_check_discord_payload
from triage_agent.scheduler import CheckScheduler
from triage_agent.settings import Settings
from triage_agent.visual_page_checker import VisualPageChecker
from triage_agent.wordpress_runtime import EnvironmentSecretLoader, WordPressRuntimeChecker

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

    manifest_registry: ManifestRegistry | None = None
    scheduler: CheckScheduler | None = None

    async def on_publish(event: KumaEvent, incident: Incident) -> None:
        if scheduler is None or manifest_registry is None:
            return
        if incident.kind is not IncidentKind.CONFIRMED_OUTAGE:
            return
        page = manifest_registry.page_by_monitor_id(event.monitor_id)
        if page is not None:
            await scheduler.run_immediate_deep_check(page)

    registry = DurableIncidentRegistry(settings.state_database_path or Path(":memory:"))
    engine = TriageEngine(
        probe=probe,
        publish=publisher,
        confirmation_attempts=settings.confirmation_attempts,
        confirmation_delay_seconds=settings.confirmation_delay_seconds,
        registry=registry,
        on_publish=on_publish,
    )

    page_checker: PageChecker | None = None
    wordpress_health_checker: WordPressHealthChecker | None = None
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
        if any(page.wordpress_health for page in manifest_registry.pages()):
            wordpress_health_checker = WordPressRuntimeChecker(
                client=client,
                resolver=resolve_addresses,
                secrets=EnvironmentSecretLoader(os.environ),
            )

        async def run_fast_check(page: PageManifest) -> None:
            assert page.kuma_monitor_id is not None  # enforced by manifest validation
            probe_result = await probe(page.url)
            event = KumaEvent(
                monitor_id=page.kuma_monitor_id,
                monitor_name=page.id,
                url=page.url,
                state=EventState.UP if probe_result.ok else EventState.DOWN,
                error=probe_result.error or "",
                observed_at=datetime.now(UTC).isoformat(),
            )
            await engine.handle_event(event)

        async def run_deep_check(page: PageManifest) -> None:
            assert manifest_registry is not None
            assert page_checker is not None
            result = await run_page_check(
                page=page,
                allowed_hosts=set(manifest_registry.allowed_hosts(page.id)),
                site_id=manifest_registry.site_id(page.id),
                page_checker=page_checker,
                wordpress_health_checker=wordpress_health_checker,
                wordpress_alert_publisher=publisher,
            )
            if result["classification"] not in ("healthy", "baseline_pending"):
                try:
                    await publisher(
                        render_browser_check_discord_payload(
                            page_id=page.id,
                            failed_viewports=result["evidence"]["failed_viewports"],
                            failure_codes=result["evidence"]["failure_codes"],
                            failed_plugin_assertions=result["evidence"]["failed_plugin_assertions"],
                        )
                    )
                except DiscordPublishError:
                    logger.exception("deep_check_alert_delivery_failed page_id=%s", page.id)

        scheduler = CheckScheduler(
            manifest_registry=manifest_registry,
            run_fast_check=run_fast_check,
            run_deep_check=run_deep_check,
            global_concurrency=settings.scheduler_global_concurrency,
            site_concurrency=settings.scheduler_site_concurrency,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        scheduler_task = asyncio.create_task(scheduler.run_forever()) if scheduler else None
        yield
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        await client.aclose()
        registry.close()

    return create_app(
        engine=engine,
        webhook_token=settings.webhook_token,
        lifespan=lifespan,
        manifest_registry=manifest_registry,
        page_checker=page_checker,
        wordpress_health_checker=wordpress_health_checker,
        wordpress_alert_publisher=publisher,
        manual_check_concurrency=settings.manual_check_concurrency,
    )
