from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from triage_agent.classification import Incident, IncidentKind
from triage_agent.events import KumaEvent


class PublicationDecision(StrEnum):
    PUBLISH = "publish"
    DUPLICATE = "duplicate"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class _IncidentRecord:
    kind: IncidentKind
    observed_at: datetime


class IncidentRegistry:
    def __init__(self) -> None:
        self._records: dict[int, _IncidentRecord] = {}

    def decide(self, event: KumaEvent, incident: Incident) -> PublicationDecision:
        previous = self._records.get(event.monitor_id)
        if previous is None:
            return PublicationDecision.PUBLISH
        if event.observed_datetime < previous.observed_at:
            return PublicationDecision.STALE
        if event.observed_datetime == previous.observed_at:
            if previous.kind is incident.kind:
                return PublicationDecision.STALE
            return PublicationDecision.PUBLISH
        if previous.kind is incident.kind:
            return PublicationDecision.DUPLICATE
        return PublicationDecision.PUBLISH

    def record(self, event: KumaEvent, incident: Incident) -> None:
        previous = self._records.get(event.monitor_id)
        if previous is not None and event.observed_datetime < previous.observed_at:
            return
        self._records[event.monitor_id] = _IncidentRecord(
            kind=incident.kind,
            observed_at=event.observed_datetime,
        )
