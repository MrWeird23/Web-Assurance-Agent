import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from triage_agent.api import create_app
from triage_agent.discord import DiscordPublisher
from triage_agent.engine import Publisher, TriageEngine
from triage_agent.probes import ProbeResult, probe_url, resolve_addresses
from triage_agent.settings import Settings

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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.aclose()

    return create_app(
        engine=engine,
        webhook_token=settings.webhook_token,
        lifespan=lifespan,
    )
