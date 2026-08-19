from datetime import datetime, timedelta
from pathlib import Path

from triage_agent.classification import Incident, IncidentKind
from triage_agent.durable_registry import DurableIncidentRegistry
from triage_agent.events import EventState, KumaEvent
from triage_agent.registry import PublicationDecision


def _event(*, monitor_id: int = 10, observed_at: str = "2026-08-04 13:20:00") -> KumaEvent:
    return KumaEvent(
        monitor_id=monitor_id,
        monitor_name="Example Site",
        url="https://example.com/",
        state=EventState.DOWN,
        error="HTTP 503",
        observed_at=observed_at,
    )


def _incident(kind: IncidentKind = IncidentKind.CONFIRMED_OUTAGE) -> Incident:
    return Incident(
        kind=kind,
        confirmed=True,
        summary="Confirmed",
        recommendation="Inspect origin",
    )


def test_registry_survives_restart_and_deduplicates(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    first = DurableIncidentRegistry(database)

    reservation = first.reserve(_event(), _incident())
    assert reservation.decision is PublicationDecision.PUBLISH
    first.complete(reservation, delivered=True)
    first.close()

    restarted = DurableIncidentRegistry(database)
    second = restarted.reserve(_event(observed_at="2026-08-04 13:21:00"), _incident())

    assert second.decision is PublicationDecision.DUPLICATE
    restarted.close()


def test_atomic_reservation_allows_only_one_publisher(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    first = DurableIncidentRegistry(database)
    second = DurableIncidentRegistry(database)

    one = first.reserve(_event(), _incident())
    two = second.reserve(_event(), _incident())

    assert one.decision is PublicationDecision.PUBLISH
    assert two.decision is not PublicationDecision.PUBLISH
    first.close()
    second.close()


def test_failed_delivery_is_retryable_after_lease_expiry(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 13, 20)
    registry = DurableIncidentRegistry(
        tmp_path / "state.sqlite3",
        now=lambda: now,
        lease_seconds=30,
    )
    reservation = registry.reserve(_event(), _incident())
    registry.complete(reservation, delivered=False)

    immediate = registry.reserve(_event(), _incident())
    assert immediate.decision is not PublicationDecision.PUBLISH

    now += timedelta(seconds=31)
    retry = registry.reserve(_event(), _incident())
    assert retry.decision is PublicationDecision.PUBLISH
    registry.close()


def test_recovery_records_duration_from_last_outage(tmp_path: Path) -> None:
    registry = DurableIncidentRegistry(tmp_path / "state.sqlite3")
    down = registry.reserve(_event(observed_at="2026-08-04 13:20:00"), _incident())
    registry.complete(down, delivered=True)
    recovery = registry.reserve(
        _event(observed_at="2026-08-04 13:25:00"),
        _incident(IncidentKind.RECOVERED),
    )

    assert recovery.recovery_duration_seconds == 300
    registry.close()


def test_retention_prunes_old_completed_rows_but_keeps_watermark(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 12, 0)
    registry = DurableIncidentRegistry(
        tmp_path / "state.sqlite3",
        now=lambda: now,
        retention_days=3,
    )
    reservation = registry.reserve(_event(), _incident())
    registry.complete(reservation, delivered=True)

    now += timedelta(days=4)
    assert registry.prune() >= 1
    stale = registry.reserve(_event(observed_at="2026-08-04 13:19:00"), _incident())
    assert stale.decision is PublicationDecision.STALE
    registry.close()


def test_exact_repeat_of_delivered_event_is_stale(tmp_path: Path) -> None:
    registry = DurableIncidentRegistry(tmp_path / "state.sqlite3")
    first = registry.reserve(_event(), _incident())
    registry.complete(first, delivered=True)

    repeat = registry.reserve(_event(), _incident())

    assert repeat.decision is PublicationDecision.STALE
    registry.close()


def test_newer_transition_then_older_event_is_stale(tmp_path: Path) -> None:
    registry = DurableIncidentRegistry(tmp_path / "state.sqlite3")
    outage = registry.reserve(_event(observed_at="2026-08-04 13:20:00"), _incident())
    registry.complete(outage, delivered=True)
    recovery = registry.reserve(
        _event(observed_at="2026-08-04 13:22:00"), _incident(IncidentKind.RECOVERED)
    )
    registry.complete(recovery, delivered=True)

    stale_outage = registry.reserve(_event(observed_at="2026-08-04 13:21:00"), _incident())

    assert stale_outage.decision is PublicationDecision.STALE
    registry.close()


def test_state_transition_at_equal_timestamp_still_publishes(tmp_path: Path) -> None:
    registry = DurableIncidentRegistry(tmp_path / "state.sqlite3")
    outage = registry.reserve(_event(), _incident(IncidentKind.CONFIRMED_OUTAGE))
    registry.complete(outage, delivered=True)

    recovery = registry.reserve(_event(), _incident(IncidentKind.RECOVERED))

    assert recovery.decision is PublicationDecision.PUBLISH
    registry.close()


def test_schema_migration_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    registry = DurableIncidentRegistry(database)
    assert registry.schema_version() == 1
    registry.close()

    restarted = DurableIncidentRegistry(database)
    assert restarted.schema_version() == 1
    restarted.close()
