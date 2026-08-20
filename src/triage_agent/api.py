import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request

from triage_agent import __version__
from triage_agent.browser_checks import (
    BrowserEvidence,
    classification_confidence,
    classification_next_action,
    classify_check,
    evaluate_browser_evidence,
)
from triage_agent.discord import DiscordPublishError
from triage_agent.manifests import ManifestRegistry, PageManifest, ViewportManifest
from triage_agent.wordpress_health import WordPressHealthResult
from triage_agent.wordpress_reporting import (
    render_wordpress_health_discord_payload,
    wordpress_health_failure_codes,
)

MAX_WEBHOOK_BODY_BYTES = 65_536
AlertPublisher = Callable[[dict[str, Any]], Awaitable[None]]
logger = logging.getLogger(__name__)


class Engine(Protocol):
    async def handle(self, payload: dict[str, Any]) -> Any: ...


class PageChecker(Protocol):
    async def run(
        self,
        *,
        page: PageManifest,
        viewport: ViewportManifest,
        allowed_hosts: set[str],
    ) -> BrowserEvidence: ...


class WordPressHealthChecker(Protocol):
    async def run(
        self,
        *,
        endpoint: str,
        site_id: str,
        token_secret_ref: str,
        allowed_hosts: set[str],
    ) -> WordPressHealthResult: ...


def create_app(
    *,
    engine: Engine,
    webhook_token: str,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    manifest_registry: ManifestRegistry | None = None,
    page_checker: PageChecker | None = None,
    wordpress_health_checker: WordPressHealthChecker | None = None,
    wordpress_alert_publisher: AlertPublisher | None = None,
    manual_check_concurrency: int = 1,
) -> FastAPI:
    app = FastAPI(
        title="Web Assurance Agent",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "Web Assurance Agent",
            "status": "ok",
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/uptime-kuma", status_code=202)
    async def uptime_kuma_webhook(
        request: Request,
    ) -> dict[str, str]:
        x_triage_token = request.headers.get("X-Triage-Token")
        if x_triage_token is None or not secrets.compare_digest(
            x_triage_token,
            webhook_token,
        ):
            raise HTTPException(status_code=401, detail="Invalid webhook token")
        payload = await _read_json_object(request)
        try:
            await engine.handle(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except DiscordPublishError as exc:
            raise HTTPException(status_code=503, detail="Report delivery unavailable") from exc
        return {"status": "accepted"}

    if manifest_registry is not None and page_checker is not None:
        manual_check_slots = asyncio.Queue[None](maxsize=manual_check_concurrency)
        for _ in range(manual_check_concurrency):
            manual_check_slots.put_nowait(None)

        @app.post("/checks/pages/{page_id}")
        async def check_page(page_id: str, request: Request) -> dict[str, Any]:
            _authenticate(request, webhook_token)
            try:
                page = manifest_registry.page(page_id)
                allowed_hosts = set(manifest_registry.allowed_hosts(page_id))
                site_id = manifest_registry.site_id(page_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Unknown page") from exc

            try:
                manual_check_slots.get_nowait()
            except asyncio.QueueEmpty as error:
                raise HTTPException(
                    status_code=429,
                    detail="Manual check capacity exhausted",
                    headers={"Retry-After": "1"},
                ) from error

            try:
                return await run_page_check(
                    page=page,
                    allowed_hosts=allowed_hosts,
                    site_id=site_id,
                    page_checker=page_checker,
                    wordpress_health_checker=wordpress_health_checker,
                    wordpress_alert_publisher=wordpress_alert_publisher,
                )
            finally:
                manual_check_slots.put_nowait(None)

    return app


async def run_page_check(
    *,
    page: PageManifest,
    allowed_hosts: set[str],
    site_id: str,
    page_checker: "PageChecker",
    wordpress_health_checker: "WordPressHealthChecker | None",
    wordpress_alert_publisher: AlertPublisher | None,
) -> dict[str, Any]:
    """Run every viewport + WordPress health check for one page; shared by the
    manual /checks/pages endpoint and the background scheduler's deep checks."""
    wordpress_health = []
    evidence = [
        await page_checker.run(page=page, viewport=viewport, allowed_hosts=allowed_hosts)
        for viewport in page.viewports
    ]
    if wordpress_health_checker is not None:
        for health_check in page.wordpress_health:
            health = await wordpress_health_checker.run(
                endpoint=health_check.endpoint,
                site_id=site_id,
                token_secret_ref=health_check.token_secret_ref,
                allowed_hosts=allowed_hosts,
            )
            wordpress_health.append(_wordpress_health_summary(health_check.id, health))
            if wordpress_health_failure_codes(health):
                if wordpress_alert_publisher is None:
                    logger.info(
                        "wordpress_health_alert_suppressed page_id=%s check_id=%s",
                        page.id,
                        health_check.id,
                    )
                else:
                    try:
                        await wordpress_alert_publisher(
                            render_wordpress_health_discord_payload(
                                page_id=page.id,
                                check_id=health_check.id,
                                result=health,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "wordpress_health_alert_delivery_failed page_id=%s check_id=%s",
                            page.id,
                            health_check.id,
                        )
    evaluations = [evaluate_browser_evidence(item) for item in evidence]
    failed_viewports = [
        item.device_profile
        for item, evaluation in zip(evidence, evaluations, strict=True)
        if not evaluation.healthy
    ]
    failure_codes = sorted(
        {
            *(finding.code for evaluation in evaluations for finding in evaluation.failures),
            *(code for summary in wordpress_health for code in summary["failure_codes"]),
        }
    )
    failed_plugin_assertions = sorted(
        {
            result.assertion_id
            for item in evidence
            for result in item.plugin_assertion_results
            if not result.satisfied
        }
    )
    baseline_pending = any(
        finding.code == "baseline_pending"
        for evaluation in evaluations
        for finding in evaluation.information
    )
    classification = classify_check(failure_codes, baseline_pending=baseline_pending)
    viewports = [
        {
            "device_profile": item.device_profile,
            "classification": classify_check(
                [finding.code for finding in evaluation.failures],
                baseline_pending=any(
                    finding.code == "baseline_pending" for finding in evaluation.information
                ),
            ),
            "failure_codes": sorted(finding.code for finding in evaluation.failures),
            "console_error_count": len(item.console_errors) + len(item.page_exceptions),
            "resource_failure_count": len(item.resource_failures),
            "screenshot": item.screenshot.path if item.screenshot is not None else None,
            "visual_status": item.visual_assurance.status if item.visual_assurance else None,
        }
        for item, evaluation in zip(evidence, evaluations, strict=True)
    ]
    return {
        "check_id": secrets.token_hex(16),
        "page_id": page.id,
        "site_id": site_id,
        "kuma_monitor_id": page.kuma_monitor_id,
        "classification": classification,
        "confidence": classification_confidence(classification),
        "next_action": classification_next_action(classification),
        "evidence": {
            "viewports_checked": len(evidence),
            "failed_viewports": failed_viewports,
            "failure_codes": failure_codes,
            "failed_plugin_assertions": failed_plugin_assertions,
        },
        "viewports": viewports,
        "artifacts": [item.screenshot.path for item in evidence if item.screenshot is not None],
        "wordpress_health": wordpress_health,
    }


def _wordpress_health_summary(
    check_id: str,
    health: WordPressHealthResult,
) -> dict[str, Any]:
    failure_codes = wordpress_health_failure_codes(health)
    return {
        "id": check_id,
        "ok": health.ok,
        "core_version": health.core_version,
        "core_update_available": health.core_update_available,
        "plugin_updates": list(health.plugin_updates),
        "site_health_status": health.site_health_status,
        "critical_test_count": health.critical_test_count,
        "overdue_cron_count": health.overdue_cron_count,
        "failing_cron_count": health.failing_cron_count,
        "rest_api_ok": health.rest_api_ok,
        "fatal_error_codes": list(health.fatal_error_codes),
        "error_code": health.error_code,
        "failure_codes": list(failure_codes),
    }


def _authenticate(request: Request, webhook_token: str) -> None:
    supplied_token = request.headers.get("X-Triage-Token")
    if supplied_token is None or not secrets.compare_digest(supplied_token, webhook_token):
        raise HTTPException(status_code=401, detail="Invalid webhook token")


async def _read_json_object(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if declared_length > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload too large")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON payload must be an object")
    return payload
