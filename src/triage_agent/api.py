import asyncio
import io
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from PIL import Image, UnidentifiedImageError

from triage_agent import __version__
from triage_agent.admin_ui import ADMIN_HTML
from triage_agent.baselines import BaselineStore
from triage_agent.browser_checks import (
    BrowserEvidence,
    classification_confidence,
    classification_next_action,
    classify_check,
    evaluate_browser_evidence,
)
from triage_agent.crawler import CrawlResult, PageSuggestion
from triage_agent.discord import DiscordPublishError
from triage_agent.manifest_store import ManifestStore
from triage_agent.manifests import ManifestRegistry, PageManifest, ViewportManifest
from triage_agent.probes import ProbeResult
from triage_agent.settings_store import RuntimeSettings, SettingsStore
from triage_agent.wordpress_health import WordPressHealthResult
from triage_agent.wordpress_reporting import (
    render_wordpress_health_discord_payload,
    wordpress_health_failure_codes,
)

MAX_WEBHOOK_BODY_BYTES = 65_536
MAX_ADMIN_BODY_BYTES = 1_048_576
AlertPublisher = Callable[[dict[str, Any]], Awaitable[None]]
AdminProbe = Callable[[str, set[str]], Awaitable[ProbeResult]]
AdminSuggest = Callable[[str, set[str]], Awaitable[PageSuggestion]]
AdminReload = Callable[[], Awaitable[None]]
AdminApplySettings = Callable[[RuntimeSettings, RuntimeSettings], Awaitable[bool]]
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


class SiteCrawler(Protocol):
    def crawl(
        self,
        *,
        start_url: str,
        allowed_hosts: set[str],
        config: Any,
    ) -> AsyncIterator[CrawlResult]: ...


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
    manifest_store: ManifestStore | None = None,
    admin_probe: AdminProbe | None = None,
    admin_suggest: AdminSuggest | None = None,
    admin_reload_scheduler: AdminReload | None = None,
    settings_store: SettingsStore | None = None,
    admin_apply_settings: AdminApplySettings | None = None,
    baseline_store: BaselineStore | None = None,
    site_crawler: SiteCrawler | None = None,
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

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_shell() -> str:
        return ADMIN_HTML

    if manifest_store is not None:

        def require_admin(request: Request) -> None:
            authorization = request.headers.get("Authorization", "")
            bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
            supplied = bearer or request.headers.get("X-Triage-Token", "")
            if not supplied or not secrets.compare_digest(supplied, webhook_token):
                raise HTTPException(status_code=401, detail="Invalid administrative token")

        @app.get("/admin/api/manifest", include_in_schema=False)
        async def read_admin_manifest(request: Request) -> dict[str, Any]:
            require_admin(request)
            return manifest_store.read()

        @app.put("/admin/api/manifest", include_in_schema=False)
        async def replace_admin_manifest(request: Request) -> dict[str, Any]:
            require_admin(request)
            content_length = request.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_ADMIN_BODY_BYTES:
                        raise HTTPException(status_code=413, detail="Manifest payload is too large")
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
            body = await request.body()
            if len(body) > MAX_ADMIN_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Manifest payload is too large")
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("Manifest must be an object")
                manifest_store.replace(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Unable to write site manifest: {exc}",
                ) from exc
            if admin_reload_scheduler is not None:
                await admin_reload_scheduler()
                return {"status": "saved", "restart_required": False}
            return {"status": "saved", "restart_required": True}

        if settings_store is not None:

            @app.get("/admin/api/settings", include_in_schema=False)
            async def read_admin_settings(request: Request) -> dict[str, Any]:
                require_admin(request)
                return settings_store.settings.as_dict()

            @app.put("/admin/api/settings", include_in_schema=False)
            async def replace_admin_settings(request: Request) -> dict[str, Any]:
                require_admin(request)
                payload = await _read_json_object(request)
                previous = settings_store.settings
                try:
                    new_settings = settings_store.replace(payload)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                except OSError as exc:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Unable to write settings: {exc}",
                    ) from exc
                restart_required = True
                if admin_apply_settings is not None:
                    restart_required = await admin_apply_settings(previous, new_settings)
                return {"status": "saved", "restart_required": restart_required}

        @app.post("/admin/api/pages/{page_id}/test-link", include_in_schema=False)
        async def test_admin_link(page_id: str, request: Request) -> dict[str, Any]:
            require_admin(request)
            if admin_probe is None:
                raise HTTPException(status_code=503, detail="Link testing is not configured")
            page, allowed_hosts, _ = _admin_page(manifest_store, page_id)
            result = await admin_probe(page.url, allowed_hosts)
            return {
                "ok": result.ok,
                "status_code": result.status_code,
                "latency_ms": result.latency_ms,
                "final_url": result.final_url,
                "error": result.error,
            }

        @app.post("/admin/api/suggest-page", include_in_schema=False)
        async def suggest_admin_page(request: Request) -> dict[str, Any]:
            require_admin(request)
            if admin_suggest is None:
                raise HTTPException(status_code=503, detail="Page suggestions are not configured")
            body = await _read_json_object(request)
            url = body.get("url")
            if not isinstance(url, str) or not url:
                raise HTTPException(status_code=422, detail="url is required")
            host = urlsplit(url).hostname
            if not host:
                raise HTTPException(status_code=422, detail="url must include a host")
            result = await admin_suggest(url, {host})
            return {
                "ok": result.ok,
                "title": result.title,
                "has_main": result.has_main,
                "error": result.error,
            }

        @app.post("/admin/api/pages/{page_id}/run-tests", include_in_schema=False)
        async def run_admin_tests(page_id: str, request: Request) -> StreamingResponse:
            require_admin(request)
            if page_checker is None:
                raise HTTPException(status_code=503, detail="Page testing is not configured")
            page, allowed_hosts, site_id = _admin_page(manifest_store, page_id)

            async def events() -> Any:
                yield _ndjson({"event": "start", "page_id": page.id})
                evidence: list[BrowserEvidence] = []
                for viewport in page.viewports:
                    item = await page_checker.run(
                        page=page, viewport=viewport, allowed_hosts=allowed_hosts
                    )
                    evidence.append(item)
                    evaluation = evaluate_browser_evidence(
                        item, ignore_console_errors=page.ignore_console_errors
                    )
                    yield _ndjson(
                        {
                            "event": "viewport",
                            "viewport_id": viewport.id,
                            "status": "passed" if evaluation.healthy else "failed",
                            "duration_ms": item.duration_ms,
                            "failure_codes": sorted(
                                {finding.code for finding in evaluation.failures}
                            ),
                            "information_codes": sorted(
                                {finding.code for finding in evaluation.information}
                            ),
                            "console_errors": sorted(set(item.console_errors)),
                        }
                    )
                result = await _summarize_page_check(
                    page=page,
                    evidence=evidence,
                    allowed_hosts=allowed_hosts,
                    site_id=site_id,
                    wordpress_health_checker=wordpress_health_checker,
                    wordpress_alert_publisher=wordpress_alert_publisher,
                )
                yield _ndjson({"event": "result", **result})

            return StreamingResponse(events(), media_type="application/x-ndjson")

        @app.post("/admin/api/run-full-triage", include_in_schema=False)
        async def run_admin_full_triage(request: Request) -> StreamingResponse:
            require_admin(request)
            if page_checker is None:
                raise HTTPException(status_code=503, detail="Page testing is not configured")
            registry = manifest_store.registry
            pages = registry.pages()

            async def events() -> Any:
                yield _ndjson({"event": "triage_start", "pages_total": len(pages)})
                page_results: list[dict[str, Any]] = []
                crawl_failures = 0
                for page in pages:
                    allowed_hosts = set(registry.allowed_hosts(page.id))
                    site_id = registry.site_id(page.id)
                    yield _ndjson(
                        {"event": "page_start", "page_id": page.id, "site_id": site_id}
                    )
                    evidence: list[BrowserEvidence] = []
                    for viewport in page.viewports:
                        item = await page_checker.run(
                            page=page, viewport=viewport, allowed_hosts=allowed_hosts
                        )
                        evidence.append(item)
                        evaluation = evaluate_browser_evidence(
                            item, ignore_console_errors=page.ignore_console_errors
                        )
                        yield _ndjson(
                            {
                                "event": "viewport",
                                "page_id": page.id,
                                "viewport_id": viewport.id,
                                "status": "passed" if evaluation.healthy else "failed",
                                "duration_ms": item.duration_ms,
                                "failure_codes": sorted(
                                    {finding.code for finding in evaluation.failures}
                                ),
                                "information_codes": sorted(
                                    {finding.code for finding in evaluation.information}
                                ),
                                "console_errors": sorted(set(item.console_errors)),
                            }
                        )
                    result = await _summarize_page_check(
                        page=page,
                        evidence=evidence,
                        allowed_hosts=allowed_hosts,
                        site_id=site_id,
                        wordpress_health_checker=wordpress_health_checker,
                        wordpress_alert_publisher=wordpress_alert_publisher,
                    )
                    page_results.append(result)
                    yield _ndjson({"event": "page_result", **result})

                    if page.crawl.enabled and site_crawler is not None:
                        yield _ndjson(
                            {
                                "event": "crawl_start",
                                "page_id": page.id,
                                "max_pages": page.crawl.max_pages,
                                "max_depth": page.crawl.max_depth,
                            }
                        )
                        pages_checked = 0
                        pages_failed = 0
                        async for crawl_result in site_crawler.crawl(
                            start_url=page.url,
                            allowed_hosts=allowed_hosts,
                            config=page.crawl,
                        ):
                            pages_checked += 1
                            if not crawl_result.healthy:
                                pages_failed += 1
                            yield _ndjson(
                                {
                                    "event": "crawl_page",
                                    "page_id": page.id,
                                    "url": crawl_result.url,
                                    "final_url": crawl_result.final_url,
                                    "depth": crawl_result.depth,
                                    "status_code": crawl_result.status_code,
                                    "healthy": crawl_result.healthy,
                                    "error": crawl_result.error,
                                }
                            )
                        crawl_failures += pages_failed
                        yield _ndjson(
                            {
                                "event": "crawl_result",
                                "page_id": page.id,
                                "pages_checked": pages_checked,
                                "pages_failed": pages_failed,
                                "classification": (
                                    "healthy" if pages_failed == 0 else "failed"
                                ),
                            }
                        )

                healthy = sum(
                    result["classification"] == "healthy" for result in page_results
                )
                yield _ndjson(
                    {
                        "event": "triage_result",
                        "classification": (
                            "healthy"
                            if healthy == len(page_results) and crawl_failures == 0
                            else "failed"
                        ),
                        "pages_total": len(page_results),
                        "pages_healthy": healthy,
                        "pages_failed": len(page_results) - healthy,
                    }
                )

            return StreamingResponse(events(), media_type="application/x-ndjson")

        @app.put(
            "/admin/api/pages/{page_id}/baselines/{viewport_id}", include_in_schema=False
        )
        async def upload_admin_baseline(
            page_id: str, viewport_id: str, request: Request
        ) -> dict[str, Any]:
            require_admin(request)
            if baseline_store is None:
                raise HTTPException(status_code=503, detail="Visual baselines are not configured")
            page, _, _ = _admin_page(manifest_store, page_id)
            if viewport_id not in {viewport.id for viewport in page.viewports}:
                raise HTTPException(status_code=404, detail="Unknown viewport")
            operator = request.headers.get("X-Operator-Label", "").strip()
            if not operator:
                raise HTTPException(status_code=422, detail="Operator label is required")
            png = await request.body()
            if len(png) > MAX_ADMIN_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Screenshot is too large")
            try:
                with Image.open(io.BytesIO(png)) as image:
                    if image.format != "PNG":
                        raise ValueError
                    image.verify()
            except (UnidentifiedImageError, OSError, ValueError):
                raise HTTPException(status_code=422, detail="A valid PNG is required") from None
            now = datetime.now(UTC)
            baseline_store.capture(
                page_id=page_id, viewport_id=viewport_id, png_bytes=png, captured_at=now
            )
            record = baseline_store.approve(
                page_id=page_id,
                viewport_id=viewport_id,
                operator_label=operator,
                approved_at=now,
            )
            return {"status": record.status, "sha256": record.sha256}

        @app.post(
            "/admin/api/pages/{page_id}/baselines/{viewport_id}/capture",
            include_in_schema=False,
        )
        async def capture_admin_baseline(
            page_id: str, viewport_id: str, request: Request
        ) -> dict[str, Any]:
            require_admin(request)
            if baseline_store is None or page_checker is None:
                raise HTTPException(status_code=503, detail="Visual capture is not configured")
            page, allowed_hosts, _ = _admin_page(manifest_store, page_id)
            viewport = next(
                (item for item in page.viewports if item.id == viewport_id), None
            )
            if viewport is None:
                raise HTTPException(status_code=404, detail="Unknown viewport")
            evidence = await page_checker.run(
                page=page, viewport=viewport, allowed_hosts=allowed_hosts
            )
            if evidence.screenshot is None:
                raise HTTPException(
                    status_code=502, detail="The browser did not produce a screenshot"
                )
            try:
                png = Path(evidence.screenshot.path).read_bytes()
            except OSError:
                raise HTTPException(
                    status_code=502, detail="The captured screenshot is unavailable"
                ) from None
            record = baseline_store.capture(
                page_id=page_id,
                viewport_id=viewport_id,
                png_bytes=png,
                captured_at=datetime.now(UTC),
            )
            return {"status": record.status, "sha256": record.sha256}

        @app.get(
            "/admin/api/pages/{page_id}/baselines/{viewport_id}/pending",
            include_in_schema=False,
        )
        async def preview_admin_baseline(
            page_id: str, viewport_id: str, request: Request
        ) -> Response:
            require_admin(request)
            if baseline_store is None:
                raise HTTPException(status_code=503, detail="Visual baselines are not configured")
            page, _, _ = _admin_page(manifest_store, page_id)
            if viewport_id not in {viewport.id for viewport in page.viewports}:
                raise HTTPException(status_code=404, detail="Unknown viewport")
            png = baseline_store.pending_png(page_id=page_id, viewport_id=viewport_id)
            if png is None:
                raise HTTPException(status_code=404, detail="No pending reference screenshot")
            return Response(content=png, media_type="image/png")

        @app.post(
            "/admin/api/pages/{page_id}/baselines/{viewport_id}/approve",
            include_in_schema=False,
        )
        async def approve_admin_baseline(
            page_id: str, viewport_id: str, request: Request
        ) -> dict[str, Any]:
            require_admin(request)
            if baseline_store is None:
                raise HTTPException(status_code=503, detail="Visual baselines are not configured")
            page, _, _ = _admin_page(manifest_store, page_id)
            if viewport_id not in {viewport.id for viewport in page.viewports}:
                raise HTTPException(status_code=404, detail="Unknown viewport")
            operator = request.headers.get("X-Operator-Label", "").strip()
            if not operator:
                raise HTTPException(status_code=422, detail="Operator label is required")
            try:
                record = baseline_store.approve(
                    page_id=page_id,
                    viewport_id=viewport_id,
                    operator_label=operator,
                    approved_at=datetime.now(UTC),
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"status": record.status, "sha256": record.sha256}

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
            # manifest_store.registry reflects the latest saved manifest even
            # when it was updated after this app was built; fall back to the
            # snapshot only if there's no live store to read from.
            current_registry = (
                manifest_store.registry if manifest_store is not None else manifest_registry
            )
            try:
                page = current_registry.page(page_id)
                allowed_hosts = set(current_registry.allowed_hosts(page_id))
                site_id = current_registry.site_id(page_id)
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


def _admin_page(
    store: ManifestStore, page_id: str
) -> tuple[PageManifest, set[str], str]:
    try:
        page = store.registry.page(page_id)
        allowed_hosts = set(store.registry.allowed_hosts(page_id))
        site_id = store.registry.site_id(page_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown page") from None
    return page, allowed_hosts, site_id


def _ndjson(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


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
    evidence = [
        await page_checker.run(page=page, viewport=viewport, allowed_hosts=allowed_hosts)
        for viewport in page.viewports
    ]
    return await _summarize_page_check(
        page=page,
        evidence=evidence,
        allowed_hosts=allowed_hosts,
        site_id=site_id,
        wordpress_health_checker=wordpress_health_checker,
        wordpress_alert_publisher=wordpress_alert_publisher,
    )


async def _summarize_page_check(
    *,
    page: PageManifest,
    evidence: list[BrowserEvidence],
    allowed_hosts: set[str],
    site_id: str,
    wordpress_health_checker: "WordPressHealthChecker | None",
    wordpress_alert_publisher: AlertPublisher | None,
) -> dict[str, Any]:
    wordpress_health = []
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
    evaluations = [
        evaluate_browser_evidence(item, ignore_console_errors=page.ignore_console_errors)
        for item in evidence
    ]
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
