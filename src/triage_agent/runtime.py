import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
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
from triage_agent.crawler import SafeCrawlFetcher, SiteCrawler, suggest_page
from triage_agent.discord import DiscordPublishError, validate_discord_webhook_url
from triage_agent.durable_registry import DurableIncidentRegistry
from triage_agent.engine import Publisher, TriageEngine
from triage_agent.events import EventState, KumaEvent
from triage_agent.manifest_store import ManifestStore
from triage_agent.manifests import (
    ManifestRegistry,
    PageManifest,
    is_within_maintenance_window,
)
from triage_agent.probes import ProbeResult, probe_url, resolve_addresses
from triage_agent.reporting import render_browser_check_discord_payload
from triage_agent.scheduler import CheckScheduler
from triage_agent.settings import Settings
from triage_agent.settings_store import RuntimeSettings, SettingsStore
from triage_agent.visual_page_checker import VisualPageChecker
from triage_agent.wordpress_runtime import EnvironmentSecretLoader, WordPressRuntimeChecker

logger = logging.getLogger(__name__)


def build_app(settings: Settings) -> FastAPI:
    # Env vars remain the defaults; config/settings.yaml (admin-editable, next
    # to sites.yaml) overrides them once it exists, same precedence as the
    # site manifest.
    env_settings = RuntimeSettings(
        discord_webhook_url=settings.discord_webhook_url,
        confirmation_attempts=settings.confirmation_attempts,
        confirmation_delay_seconds=settings.confirmation_delay_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
        manual_check_concurrency=settings.manual_check_concurrency,
        scheduler_global_concurrency=settings.scheduler_global_concurrency,
        scheduler_site_concurrency=settings.scheduler_site_concurrency,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    if env_settings.discord_webhook_url is not None:
        validate_discord_webhook_url(env_settings.discord_webhook_url)
    settings_store: SettingsStore | None = (
        SettingsStore(settings.settings_path, defaults=env_settings)
        if settings.settings_path is not None
        else None
    )

    def current_settings() -> RuntimeSettings:
        return settings_store.settings if settings_store is not None else env_settings

    logging.getLogger().setLevel(current_settings().log_level)
    client = httpx.AsyncClient(timeout=current_settings().request_timeout_seconds)

    async def probe(url: str) -> ProbeResult:
        return await probe_url(
            url,
            allowed_hosts=set(settings.allowed_hosts),
            client=client,
            resolver=resolve_addresses,
        )

    async def publish(payload: dict[str, Any]) -> None:
        url = current_settings().discord_webhook_url
        if not url:
            logger.info("dry_run_discord_payload=%s", json.dumps(payload, sort_keys=True))
            return
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError:
            raise DiscordPublishError("Discord webhook delivery failed") from None

    publisher: Publisher = publish

    manifest_registry: ManifestRegistry | None = None
    manifest_store: ManifestStore | None = None
    scheduler: CheckScheduler | None = None
    scheduler_task: asyncio.Task[None] | None = None
    reload_scheduler: Callable[[], Awaitable[None]] | None = None

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
        confirmation_attempts=current_settings().confirmation_attempts,
        confirmation_delay_seconds=current_settings().confirmation_delay_seconds,
        registry=registry,
        on_publish=on_publish,
    )

    page_checker: PageChecker | None = None
    baseline_store: BaselineStore | None = None
    wordpress_health_checker: WordPressHealthChecker | None = None
    if settings.site_manifest_path is not None:
        manifest_store = ManifestStore(
            settings.site_manifest_path,
            permitted_hosts=set(settings.allowed_hosts),
        )
        manifest_registry = manifest_store.registry
        browser_checker = PlaywrightBrowserRunner(
            resolver=resolve_addresses,
            artifact_directory=settings.browser_artifact_directory,
        )
        if settings.visual_baseline_directory is not None:
            baseline_store = BaselineStore(settings.visual_baseline_directory)
            page_checker = VisualPageChecker(
                checker=browser_checker,
                baseline_store=baseline_store,
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
            now = datetime.now(UTC)
            event = KumaEvent(
                monitor_id=page.kuma_monitor_id,
                monitor_name=page.id,
                url=page.url,
                state=EventState.UP if probe_result.ok else EventState.DOWN,
                error=probe_result.error or "",
                observed_at=now.isoformat(),
            )
            await engine.handle_event(
                event, suppress_publish=is_within_maintenance_window(page.maintenance_windows, now)
            )

        async def run_deep_check(page: PageManifest) -> None:
            assert manifest_registry is not None
            assert page_checker is not None
            in_maintenance_window = is_within_maintenance_window(
                page.maintenance_windows, datetime.now(UTC)
            )
            result = await run_page_check(
                page=page,
                allowed_hosts=set(manifest_registry.allowed_hosts(page.id)),
                site_id=manifest_registry.site_id(page.id),
                page_checker=page_checker,
                wordpress_health_checker=wordpress_health_checker,
                wordpress_alert_publisher=None if in_maintenance_window else publisher,
            )
            if result["classification"] not in ("healthy", "baseline_pending"):
                if in_maintenance_window:
                    logger.info("maintenance_window_suppressed_alert page_id=%s", page.id)
                else:
                    try:
                        await publisher(
                            render_browser_check_discord_payload(
                                page_id=page.id,
                                failed_viewports=result["evidence"]["failed_viewports"],
                                failure_codes=result["evidence"]["failure_codes"],
                                failed_plugin_assertions=result["evidence"][
                                    "failed_plugin_assertions"
                                ],
                            )
                        )
                    except DiscordPublishError:
                        logger.exception("deep_check_alert_delivery_failed page_id=%s", page.id)

        scheduler = CheckScheduler(
            manifest_registry=manifest_registry,
            run_fast_check=run_fast_check,
            run_deep_check=run_deep_check,
            global_concurrency=current_settings().scheduler_global_concurrency,
            site_concurrency=current_settings().scheduler_site_concurrency,
        )

        async def reload_scheduler_impl() -> None:
            # Admin manifest *or settings* save calls this so a new/changed
            # site's fast/deep checks -- or a new concurrency setting --
            # apply immediately, no container restart needed. The scheduler
            # snapshots its page loops (and concurrency) at construction, so
            # a live edit means: stop the old loop set, rebuild, start a new
            # one.
            nonlocal manifest_registry, scheduler, scheduler_task
            assert manifest_store is not None
            if scheduler_task is not None:
                scheduler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler_task
            manifest_registry = manifest_store.registry
            scheduler = CheckScheduler(
                manifest_registry=manifest_registry,
                run_fast_check=run_fast_check,
                run_deep_check=run_deep_check,
                global_concurrency=current_settings().scheduler_global_concurrency,
                site_concurrency=current_settings().scheduler_site_concurrency,
            )
            scheduler_task = asyncio.create_task(scheduler.run_forever())

        reload_scheduler = reload_scheduler_impl

    async def apply_settings(previous: RuntimeSettings, new: RuntimeSettings) -> bool:
        # Admin settings save calls this. Log level and confirmation tuning
        # apply in place; concurrency rides the scheduler reload above.
        # Timeout/manual-check-concurrency are baked into the httpx client
        # and a fixed-size queue at startup, so those two still need a
        # restart -- reported back to the admin UI, not silently dropped.
        logging.getLogger().setLevel(new.log_level)
        engine.set_confirmation(new.confirmation_attempts, new.confirmation_delay_seconds)
        if reload_scheduler is not None:
            await reload_scheduler()
        return new.restart_required_vs(previous)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal scheduler_task
        scheduler_task = asyncio.create_task(scheduler.run_forever()) if scheduler else None
        yield
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        await client.aclose()
        registry.close()

    crawl_fetcher = SafeCrawlFetcher(client=client, resolver=resolve_addresses)

    return create_app(
        engine=engine,
        webhook_token=settings.webhook_token,
        lifespan=lifespan,
        manifest_registry=manifest_registry,
        page_checker=page_checker,
        wordpress_health_checker=wordpress_health_checker,
        wordpress_alert_publisher=publisher,
        manual_check_concurrency=current_settings().manual_check_concurrency,
        manifest_store=manifest_store,
        admin_reload_scheduler=reload_scheduler,
        settings_store=settings_store,
        admin_apply_settings=apply_settings if settings_store is not None else None,
        admin_probe=lambda url, allowed_hosts: probe_url(
            url, allowed_hosts=allowed_hosts, client=client, resolver=resolve_addresses
        ),
        admin_suggest=lambda url, allowed_hosts: suggest_page(
            url, allowed_hosts=allowed_hosts, fetch=crawl_fetcher
        ),
        baseline_store=baseline_store,
        site_crawler=SiteCrawler(fetch=crawl_fetcher),
    )
