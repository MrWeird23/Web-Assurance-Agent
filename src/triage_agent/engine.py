import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from triage_agent.classification import Incident, classify_incident
from triage_agent.durable_registry import DurableIncidentRegistry
from triage_agent.events import EventState, KumaEvent, parse_kuma_event
from triage_agent.probes import ProbeResult
from triage_agent.registry import PublicationDecision
from triage_agent.reporting import render_discord_payload

logger = logging.getLogger(__name__)

Probe = Callable[[str], Awaitable[ProbeResult]]
Publisher = Callable[[dict[str, Any]], Awaitable[None]]
Sleeper = Callable[[float], Awaitable[None]]
IncidentHook = Callable[[KumaEvent, Incident], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    event: KumaEvent
    incident: Incident
    probes: list[ProbeResult]
    discord_payload: dict[str, Any]


class TriageEngine:
    def __init__(
        self,
        *,
        probe: Probe,
        publish: Publisher,
        confirmation_attempts: int = 2,
        confirmation_delay_seconds: float = 0,
        sleeper: Sleeper = asyncio.sleep,
        registry: DurableIncidentRegistry | None = None,
        on_publish: IncidentHook | None = None,
    ) -> None:
        if confirmation_attempts < 1:
            raise ValueError("confirmation_attempts must be at least 1")
        if confirmation_delay_seconds < 0:
            raise ValueError("confirmation_delay_seconds cannot be negative")
        self._probe = probe
        self._publish = publish
        self._confirmation_attempts = confirmation_attempts
        self._confirmation_delay_seconds = confirmation_delay_seconds
        self._sleeper = sleeper
        self._registry = registry or DurableIncidentRegistry(Path(":memory:"))
        self._on_publish = on_publish
        self._monitor_locks: dict[int, asyncio.Lock] = {}

    async def handle(self, payload: dict[str, Any]) -> TriageOutcome:
        return await self.handle_event(parse_kuma_event(payload))

    async def handle_event(self, event: KumaEvent) -> TriageOutcome:
        monitor_lock = self._monitor_locks.setdefault(event.monitor_id, asyncio.Lock())
        async with monitor_lock:
            return await self._handle_event(event)

    async def _handle_event(self, event: KumaEvent) -> TriageOutcome:
        probes = []
        if event.state is EventState.DOWN:
            for attempt in range(self._confirmation_attempts):
                if attempt:
                    await self._sleeper(self._confirmation_delay_seconds)
                probes.append(await self._probe(event.url))
        incident = classify_incident(event, probes)
        discord_payload = render_discord_payload(event, incident, probes)
        reservation = self._registry.reserve(event, incident)
        if reservation.decision is PublicationDecision.PUBLISH:
            try:
                await self._publish(discord_payload)
            except Exception:
                self._registry.complete(reservation, delivered=False)
                raise
            self._registry.complete(reservation, delivered=True)
            if self._on_publish is not None:
                try:
                    await self._on_publish(event, incident)
                except Exception:
                    logger.exception(
                        "incident_publish_hook_failed monitor_id=%s", event.monitor_id
                    )
        return TriageOutcome(
            event=event,
            incident=incident,
            probes=probes,
            discord_payload=discord_payload,
        )
