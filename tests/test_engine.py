import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from triage_agent.classification import IncidentKind
from triage_agent.durable_registry import DurableIncidentRegistry
from triage_agent.engine import TriageEngine
from triage_agent.probes import ProbeResult


async def test_engine_confirms_down_event_and_publishes_report() -> None:
    probe_calls: list[str] = []
    published: list[dict[str, Any]] = []

    async def probe(url: str) -> ProbeResult:
        probe_calls.append(url)
        return ProbeResult(
            ok=False,
            status_code=503,
            latency_ms=100,
            final_url=url,
            error="HTTP 503",
            server="cloudflare",
        )

    async def publish(payload: dict[str, Any]) -> None:
        published.append(payload)

    engine = TriageEngine(probe=probe, publish=publish, confirmation_attempts=2)
    outcome = await engine.handle(
        {
            "heartbeat": {
                "status": 0,
                "time": "2026-08-04 13:20:00",
                "msg": "Request failed with status code 503",
            },
            "monitor": {
                "id": 11,
                "name": "Client Shop",
                "type": "http",
                "url": "https://shop.example.com/",
            },
        }
    )

    assert outcome.incident.kind is IncidentKind.CONFIRMED_OUTAGE
    assert probe_calls == ["https://shop.example.com/", "https://shop.example.com/"]
    assert len(published) == 1
    assert published[0]["embeds"][0]["title"] == "Confirmed outage"


async def test_engine_deduplicates_repeated_incident_reports() -> None:
    published: list[dict[str, Any]] = []

    async def probe(url: str) -> ProbeResult:
        return ProbeResult(False, 503, 100, url, "HTTP 503", "cloudflare")

    async def publish(payload: dict[str, Any]) -> None:
        published.append(payload)

    engine = TriageEngine(
        probe=probe,
        publish=publish,
        confirmation_attempts=1,
    )
    payload = {
        "heartbeat": {"status": 0, "time": "2026-08-04 13:20:00", "msg": "HTTP 503"},
        "monitor": {
            "id": 11,
            "name": "Client Shop",
            "type": "http",
            "url": "https://shop.example.com/",
        },
    }

    await engine.handle(payload)
    await engine.handle(payload)

    assert len(published) == 1


async def test_engine_waits_between_confirmation_attempts() -> None:
    waits: list[float] = []

    async def probe(url: str) -> ProbeResult:
        return ProbeResult(False, 503, 100, url, "HTTP 503", "cloudflare")

    async def publish(_payload: dict[str, Any]) -> None:
        return None

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    engine = TriageEngine(
        probe=probe,
        publish=publish,
        confirmation_attempts=3,
        confirmation_delay_seconds=5,
        sleeper=sleep,
    )

    await engine.handle(
        {
            "heartbeat": {"status": 0, "time": "2026-08-04 13:20:00", "msg": "HTTP 503"},
            "monitor": {"id": 12, "name": "Example", "url": "https://example.com/"},
        }
    )

    assert waits == [5, 5]


async def test_engine_retries_same_incident_after_publication_failure_once_lease_expires(
    tmp_path: Path,
) -> None:
    attempts = 0
    now = datetime(2026, 8, 4, 13, 20)

    async def probe(url: str) -> ProbeResult:
        return ProbeResult(False, 503, 100, url, "HTTP 503", "cloudflare")

    async def publish(_payload: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("delivery failed")

    registry = DurableIncidentRegistry(
        tmp_path / "state.sqlite3", now=lambda: now, lease_seconds=30
    )
    engine = TriageEngine(
        probe=probe, publish=publish, confirmation_attempts=1, registry=registry
    )
    payload = {
        "heartbeat": {"status": 0, "time": "2026-08-04 13:20:00", "msg": "HTTP 503"},
        "monitor": {"id": 12, "name": "Example", "url": "https://example.com/"},
    }

    with pytest.raises(RuntimeError, match="delivery failed"):
        await engine.handle(payload)
    await engine.handle(payload)
    assert attempts == 1  # blocked: failed reservation's lease has not expired yet

    now += timedelta(seconds=31)
    await engine.handle(payload)
    assert attempts == 2
    registry.close()


async def test_engine_does_not_publish_stale_transition_after_newer_recovery() -> None:
    published_titles: list[str] = []

    async def probe(url: str) -> ProbeResult:
        return ProbeResult(False, 503, 100, url, "HTTP 503", "cloudflare")

    async def publish(payload: dict[str, Any]) -> None:
        published_titles.append(payload["embeds"][0]["title"])

    engine = TriageEngine(probe=probe, publish=publish, confirmation_attempts=1)
    recovery = {
        "heartbeat": {"status": 1, "time": "2026-08-04 13:22:00", "msg": "200 OK"},
        "monitor": {"id": 12, "name": "Example", "url": "https://example.com/"},
    }
    stale_outage = {
        "heartbeat": {"status": 0, "time": "2026-08-04 13:20:00", "msg": "HTTP 503"},
        "monitor": {"id": 12, "name": "Example", "url": "https://example.com/"},
    }

    await engine.handle(recovery)
    await engine.handle(stale_outage)

    assert published_titles == ["Recovered"]


async def test_engine_serializes_concurrent_events_for_same_monitor() -> None:
    published: list[dict[str, Any]] = []

    async def probe(url: str) -> ProbeResult:
        await asyncio.sleep(0)
        return ProbeResult(False, 503, 100, url, "HTTP 503", "cloudflare")

    async def publish(payload: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        published.append(payload)

    engine = TriageEngine(probe=probe, publish=publish, confirmation_attempts=1)
    payload = {
        "heartbeat": {"status": 0, "time": "2026-08-04 13:20:00", "msg": "HTTP 503"},
        "monitor": {"id": 12, "name": "Example", "url": "https://example.com/"},
    }

    await asyncio.gather(engine.handle(payload), engine.handle(payload))

    assert len(published) == 1
