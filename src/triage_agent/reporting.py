from typing import Any
from urllib.parse import urlsplit

from triage_agent.classification import Incident, IncidentKind
from triage_agent.events import EventState, KumaEvent
from triage_agent.probes import ProbeResult

_TITLES = {
    IncidentKind.CONFIRMED_OUTAGE: "Confirmed outage",
    IncidentKind.MONITOR_BLOCKED: "Probable monitor blocking",
    IncidentKind.TRANSIENT_FAILURE: "Unconfirmed transient failure",
    IncidentKind.RECOVERED: "Recovered",
}
_COLORS = {
    IncidentKind.CONFIRMED_OUTAGE: 0xD83C3E,
    IncidentKind.MONITOR_BLOCKED: 0xF0B232,
    IncidentKind.TRANSIENT_FAILURE: 0xF0B232,
    IncidentKind.RECOVERED: 0x3BA55D,
}
_DESCRIPTIONS = {
    IncidentKind.CONFIRMED_OUTAGE: "Independent confirmation indicates an origin HTTP failure.",
    IncidentKind.MONITOR_BLOCKED: (
        "Kuma reported HTTP 403, but independent confirmation succeeded."
    ),
    IncidentKind.TRANSIENT_FAILURE: (
        "The reported failure could not be independently confirmed."
    ),
    IncidentKind.RECOVERED: "The monitored site is responding normally again.",
}
_RECOMMENDATIONS = {
    IncidentKind.CONFIRMED_OUTAGE: "Inspect the application or origin service immediately.",
    IncidentKind.MONITOR_BLOCKED: "Review WAF or bot-protection rules for the Kuma probe.",
    IncidentKind.TRANSIENT_FAILURE: "Continue monitoring and verify agent connectivity.",
    IncidentKind.RECOVERED: "No intervention is required.",
}
def _format_probe(probe: ProbeResult) -> str:
    status = (
        f"HTTP {probe.status_code}"
        if type(probe.status_code) is int and 100 <= probe.status_code <= 599
        else "No origin response"
    )
    details = [status]
    if type(probe.latency_ms) is int and 0 <= probe.latency_ms <= 86_400_000:
        details.append(f"{probe.latency_ms} ms")
    if probe.server:
        details.append("Server header present")
    if probe.cloudflare_ray:
        details.append("CF-Ray header present")
    return " · ".join(details)


def _safe_display_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or host is None or port not in (None, 443):
        return ""
    display_host = f"[{host}]" if ":" in host else host
    return f"https://{display_host}/"


def render_discord_payload(
    event: KumaEvent,
    incident: Incident,
    probes: list[ProbeResult],
) -> dict[str, Any]:
    confirmation = "\n".join(_format_probe(probe) for probe in probes) or "Not required"
    return {
        "username": "Web Assurance Agent",
        "embeds": [
            {
                "title": _TITLES[incident.kind],
                "url": _safe_display_url(event.url),
                "color": _COLORS[incident.kind],
                "description": _DESCRIPTIONS[incident.kind],
                "fields": [
                    {
                        "name": "Kuma evidence",
                        "value": (
                            "Kuma reported a recovery."
                            if event.state is EventState.UP
                            else "Kuma reported a failure."
                        ),
                    },
                    {"name": "Independent confirmation", "value": confirmation},
                    {
                        "name": "Recommendation",
                        "value": _RECOMMENDATIONS[incident.kind],
                    },
                ],
                "footer": {"text": f"Kuma monitor {event.monitor_id} · {event.observed_at}"},
            }
        ],
    }
