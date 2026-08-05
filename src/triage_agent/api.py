import json
import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request

from triage_agent.discord import DiscordPublishError

MAX_WEBHOOK_BODY_BYTES = 65_536


class Engine(Protocol):
    async def handle(self, payload: dict[str, Any]) -> Any: ...


def create_app(
    *,
    engine: Engine,
    webhook_token: str,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Web Assurance Agent",
        version="0.1.0",
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

    return app


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
