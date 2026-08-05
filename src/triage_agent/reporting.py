import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from triage_agent.classification import Incident, IncidentKind
from triage_agent.events import KumaEvent
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
_URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_SECRET_PATTERN = re.compile(
    r"\b(token|api[_-]?key|secret|password|passwd|signature|sig)(\s*=\s*)[^\s&;,]+",
    re.IGNORECASE,
)


def _format_probe(probe: ProbeResult) -> str:
    status = (
        f"HTTP {probe.status_code}"
        if probe.status_code is not None
        else _sanitize_evidence(probe.error or "")
    )
    details = [status or "No response"]
    if probe.latency_ms is not None:
        details.append(f"{probe.latency_ms} ms")
    if probe.server:
        details.append(f"Server: {probe.server}")
    if probe.cloudflare_ray:
        details.append(f"CF-Ray: {probe.cloudflare_ray}")
    return " · ".join(details)


def _safe_display_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or host is None:
        return ""
    display_host = f"[{host}]" if ":" in host else host
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        display_host = f"{display_host}:{port}"
    return urlunsplit((parsed.scheme, display_host, parsed.path, "", ""))


def _sanitize_evidence(text: str) -> str:
    without_url_secrets = _URL_PATTERN.sub(
        lambda match: _safe_display_url(match.group(0)) or "[REDACTED URL]",
        text,
    )
    return _SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        without_url_secrets,
    )


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
                "title": f"{_TITLES[incident.kind]}: {event.monitor_name}",
                "url": _safe_display_url(event.url),
                "color": _COLORS[incident.kind],
                "description": incident.summary,
                "fields": [
                    {
                        "name": "Kuma evidence",
                        "value": _sanitize_evidence(event.error)
                        if event.error
                        else "No detail supplied",
                    },
                    {"name": "Independent confirmation", "value": confirmation},
                    {"name": "Recommendation", "value": incident.recommendation},
                ],
                "footer": {"text": f"Kuma monitor {event.monitor_id} · {event.observed_at}"},
            }
        ],
    }
