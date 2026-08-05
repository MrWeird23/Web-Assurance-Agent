from triage_agent.classification import Incident, IncidentKind
from triage_agent.events import EventState, KumaEvent
from triage_agent.registry import IncidentRegistry, PublicationDecision


def test_registry_suppresses_repeated_classification_for_same_monitor() -> None:
    registry = IncidentRegistry()
    event = KumaEvent(
        monitor_id=10,
        monitor_name="Example Site",
        url="https://monitor.example.com/",
        state=EventState.DOWN,
        error="HTTP 503",
        observed_at="2026-08-04 13:20:00",
    )
    incident = Incident(
        kind=IncidentKind.CONFIRMED_OUTAGE,
        confirmed=True,
        summary="Confirmed",
        recommendation="Inspect origin",
    )

    assert registry.decide(event, incident) is PublicationDecision.PUBLISH
    registry.record(event, incident)
    assert registry.decide(event, incident) is PublicationDecision.STALE


def test_registry_distinguishes_newer_duplicates_from_stale_events() -> None:
    registry = IncidentRegistry()
    incident = Incident(
        kind=IncidentKind.CONFIRMED_OUTAGE,
        confirmed=True,
        summary="Confirmed",
        recommendation="Inspect origin",
    )
    first = KumaEvent(
        10,
        "Site",
        "https://example.com/",
        EventState.DOWN,
        "HTTP 503",
        "2026-08-04 13:20:00",
    )
    newer = KumaEvent(
        10,
        "Site",
        "https://example.com/",
        EventState.DOWN,
        "HTTP 503",
        "2026-08-04 13:22:00",
    )
    older = KumaEvent(
        10,
        "Site",
        "https://example.com/",
        EventState.DOWN,
        "HTTP 503",
        "2026-08-04 13:21:00",
    )

    registry.record(first, incident)

    assert registry.decide(newer, incident) is PublicationDecision.DUPLICATE
    registry.record(newer, incident)
    assert registry.decide(older, incident) is PublicationDecision.STALE


def test_registry_publishes_state_transition_with_equal_timestamp() -> None:
    registry = IncidentRegistry()
    event = KumaEvent(
        10,
        "Site",
        "https://example.com/",
        EventState.DOWN,
        "HTTP 503",
        "2026-08-04 13:20:00",
    )
    outage = Incident(
        kind=IncidentKind.CONFIRMED_OUTAGE,
        confirmed=True,
        summary="Confirmed",
        recommendation="Inspect origin",
    )
    recovery = Incident(
        kind=IncidentKind.RECOVERED,
        confirmed=True,
        summary="UP event",
        recommendation="No action",
    )

    registry.record(event, outage)

    assert registry.decide(event, recovery) is PublicationDecision.PUBLISH
