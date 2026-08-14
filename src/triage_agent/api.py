import asyncio
import json
import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request

from triage_agent import __version__
from triage_agent.browser_checks import BrowserEvidence, evaluate_browser_evidence
from triage_agent.discord import DiscordPublishError
from triage_agent.manifests import ManifestRegistry, PageManifest, ViewportManifest

MAX_WEBHOOK_BODY_BYTES = 65_536


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


def create_app(
    *,
    engine: Engine,
    webhook_token: str,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    manifest_registry: ManifestRegistry | None = None,
    page_checker: PageChecker | None = None,
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
                evidence = [
                    await page_checker.run(
                        page=page,
                        viewport=viewport,
                        allowed_hosts=allowed_hosts,
                    )
                    for viewport in page.viewports
                ]
            finally:
                manual_check_slots.put_nowait(None)
            evaluations = [evaluate_browser_evidence(item) for item in evidence]
            failed_viewports = [
                item.device_profile
                for item, evaluation in zip(evidence, evaluations, strict=True)
                if not evaluation.healthy
            ]
            failure_codes = sorted(
                {finding.code for evaluation in evaluations for finding in evaluation.failures}
            )
            return {
                "check_id": secrets.token_hex(16),
                "page_id": page.id,
                "classification": "healthy" if not failed_viewports else "failed",
                "evidence": {
                    "viewports_checked": len(evidence),
                    "failed_viewports": failed_viewports,
                    "failure_codes": failure_codes,
                },
                "artifacts": [
                    item.screenshot.path for item in evidence if item.screenshot is not None
                ],
            }

    return app


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
