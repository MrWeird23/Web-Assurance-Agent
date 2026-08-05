import re
from dataclasses import dataclass
from enum import StrEnum

from triage_agent.events import EventState, KumaEvent
from triage_agent.probes import ProbeResult

_HTTP_STATUS_PATTERN = re.compile(
    r"(?:\b(?:http(?:\s+status)?|status(?:\s+code)?)\s*[:=-]?\s*(\d{3})\b|"
    r"\b(403)\s+forbidden\b)",
    re.IGNORECASE,
)


class IncidentKind(StrEnum):
    MONITOR_BLOCKED = "monitor_blocked"
    CONFIRMED_OUTAGE = "confirmed_outage"
    RECOVERED = "recovered"
    TRANSIENT_FAILURE = "transient_failure"


@dataclass(frozen=True, slots=True)
class Incident:
    kind: IncidentKind
    confirmed: bool
    summary: str
    recommendation: str


def _reported_http_status(error: str) -> int | None:
    match = _HTTP_STATUS_PATTERN.search(error)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def classify_incident(event: KumaEvent, probes: list[ProbeResult]) -> Incident:
    if event.state is EventState.UP:
        return Incident(
            kind=IncidentKind.RECOVERED,
            confirmed=True,
            summary=f"{event.monitor_name} is responding normally again.",
            recommendation="No intervention is required.",
        )
    if _reported_http_status(event.error) == 403 and any(probe.ok for probe in probes):
        return Incident(
            kind=IncidentKind.MONITOR_BLOCKED,
            confirmed=False,
            summary="Kuma received HTTP 403, but the independent confirmation succeeded.",
            recommendation="Review WAF or bot-protection rules for the Kuma probe.",
        )
    if probes and all(
        not probe.ok
        and probe.status_code is not None
        and 400 <= probe.status_code < 600
        for probe in probes
    ):
        status_code = next(
            (probe.status_code for probe in probes if probe.status_code is not None),
            None,
        )
        detail = f"HTTP {status_code}" if status_code is not None else event.error
        return Incident(
            kind=IncidentKind.CONFIRMED_OUTAGE,
            confirmed=True,
            summary=f"Independent confirmation attempts failed with {detail}.",
            recommendation="Inspect the application or origin service immediately.",
        )
    if any(probe.ok for probe in probes):
        return Incident(
            kind=IncidentKind.TRANSIENT_FAILURE,
            confirmed=False,
            summary="Kuma reported a failure, but independent confirmation succeeded.",
            recommendation="Continue monitoring; no immediate intervention is indicated.",
        )
    if probes:
        return Incident(
            kind=IncidentKind.TRANSIENT_FAILURE,
            confirmed=False,
            summary=(
                "Independent confirmation was inconclusive because no origin response was "
                "obtained."
            ),
            recommendation="Verify agent DNS and network access, then continue monitoring.",
        )
    raise ValueError("Unable to classify incident")
