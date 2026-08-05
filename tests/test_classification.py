from triage_agent.classification import IncidentKind, classify_incident
from triage_agent.events import EventState, KumaEvent
from triage_agent.probes import ProbeResult


def test_classifies_kuma_403_as_monitor_block_when_confirmation_succeeds() -> None:
    event = KumaEvent(
        monitor_id=10,
        monitor_name="Example Site",
        url="https://monitor.example.com/",
        state=EventState.DOWN,
        error="Request failed with status code 403",
        observed_at="2026-08-04 13:14:37",
    )
    probes = [
        ProbeResult(
            ok=True,
            status_code=200,
            latency_ms=321,
            final_url="https://monitor.example.com/",
            error=None,
            server="cloudflare",
        )
    ]

    incident = classify_incident(event, probes)

    assert incident.kind is IncidentKind.MONITOR_BLOCKED
    assert incident.confirmed is False
    assert "403" in incident.summary
    assert incident.recommendation == "Review WAF or bot-protection rules for the Kuma probe."


def test_confirms_outage_when_all_confirmation_attempts_fail() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Client Shop",
        url="https://shop.example.com/",
        state=EventState.DOWN,
        error="Request failed with status code 503",
        observed_at="2026-08-04 13:20:00",
    )
    probes = [
        ProbeResult(
            ok=False,
            status_code=503,
            latency_ms=200,
            final_url="https://shop.example.com/",
            error="HTTP 503",
            server="cloudflare",
        ),
        ProbeResult(
            ok=False,
            status_code=503,
            latency_ms=180,
            final_url="https://shop.example.com/",
            error="HTTP 503",
            server="cloudflare",
        ),
    ]

    incident = classify_incident(event, probes)

    assert incident.kind is IncidentKind.CONFIRMED_OUTAGE
    assert incident.confirmed is True
    assert "503" in incident.summary
    assert incident.recommendation == "Inspect the application or origin service immediately."


def test_classifies_up_event_as_recovery_without_probing() -> None:
    event = KumaEvent(
        monitor_id=10,
        monitor_name="Example Site",
        url="https://monitor.example.com/",
        state=EventState.UP,
        error="200 - OK",
        observed_at="2026-08-04 13:15:38",
    )

    incident = classify_incident(event, [])

    assert incident.kind is IncidentKind.RECOVERED
    assert incident.confirmed is True
    assert incident.recommendation == "No intervention is required."


def test_classifies_successful_confirmation_as_transient_failure() -> None:
    event = KumaEvent(
        monitor_id=12,
        monitor_name="Example",
        url="https://example.com/",
        state=EventState.DOWN,
        error="timeout of 48000ms exceeded",
        observed_at="2026-08-04 13:30:00",
    )
    probes = [
        ProbeResult(
            ok=True,
            status_code=200,
            latency_ms=120,
            final_url="https://example.com/",
            error=None,
            server="cloudflare",
        )
    ]

    incident = classify_incident(event, probes)

    assert incident.kind is IncidentKind.TRANSIENT_FAILURE
    assert incident.confirmed is False
    assert incident.recommendation == "Continue monitoring; no immediate intervention is indicated."


def test_local_probe_failure_does_not_confirm_origin_outage() -> None:
    event = KumaEvent(
        monitor_id=12,
        monitor_name="Example",
        url="https://example.com/",
        state=EventState.DOWN,
        error="timeout of 48000ms exceeded",
        observed_at="2026-08-04 13:30:00",
    )
    probes = [
        ProbeResult(
            ok=False,
            status_code=None,
            latency_ms=120,
            final_url="https://example.com/",
            error="DNS resolution failed",
            server=None,
        )
    ]

    incident = classify_incident(event, probes)

    assert incident.kind is IncidentKind.TRANSIENT_FAILURE
    assert incident.confirmed is False
    assert "inconclusive" in incident.summary.lower()
