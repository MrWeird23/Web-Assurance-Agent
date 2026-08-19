"""Transactional SQLite incident state with restart-safe deduplication."""

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from triage_agent.classification import Incident, IncidentKind
from triage_agent.events import KumaEvent
from triage_agent.registry import PublicationDecision


@dataclass(frozen=True, slots=True)
class PublicationReservation:
    decision: PublicationDecision
    monitor_id: int
    incident_kind: IncidentKind
    observed_at: datetime
    token: str | None = None
    recovery_duration_seconds: int | None = None


class DurableIncidentRegistry:
    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        retention_days: int = 30,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._now = now or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds
        self._retention_days = retention_days
        self._migrate()

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monitor_state (
                monitor_id INTEGER PRIMARY KEY,
                incident_kind TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                outage_started_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publication_reservations (
                token TEXT PRIMARY KEY,
                monitor_id INTEGER NOT NULL,
                incident_kind TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                reserved_until TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reservations_monitor
                ON publication_reservations(monitor_id, observed_at);
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
            (_iso(self._now()),),
        )

    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"])

    def reserve(self, event: KumaEvent, incident: Incident) -> PublicationReservation:
        now = self._now()
        observed_at = event.observed_datetime
        token = uuid.uuid4().hex
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            state = connection.execute(
                "SELECT * FROM monitor_state WHERE monitor_id = ?",
                (event.monitor_id,),
            ).fetchone()
            recovery_duration = _recovery_duration(state, incident.kind, observed_at)
            decision = _decision(state, incident.kind, observed_at)
            if decision is PublicationDecision.PUBLISH:
                connection.execute(
                    """
                    DELETE FROM publication_reservations
                    WHERE monitor_id = ? AND incident_kind = ? AND observed_at = ?
                      AND delivered = 0 AND reserved_until <= ?
                    """,
                    (
                        event.monitor_id,
                        incident.kind.value,
                        _iso(observed_at),
                        _iso(now),
                    ),
                )
                active = connection.execute(
                    """
                    SELECT 1 FROM publication_reservations
                    WHERE monitor_id = ? AND incident_kind = ? AND observed_at = ?
                      AND delivered = 0 AND reserved_until > ?
                    LIMIT 1
                    """,
                    (
                        event.monitor_id,
                        incident.kind.value,
                        _iso(observed_at),
                        _iso(now),
                    ),
                ).fetchone()
                if active is not None:
                    decision = PublicationDecision.DUPLICATE
                else:
                    connection.execute(
                        """
                        INSERT INTO publication_reservations(
                            token, monitor_id, incident_kind, observed_at,
                            reserved_until, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            token,
                            event.monitor_id,
                            incident.kind.value,
                            _iso(observed_at),
                            _iso(now + timedelta(seconds=self._lease_seconds)),
                            _iso(now),
                        ),
                    )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return PublicationReservation(
            decision=decision,
            monitor_id=event.monitor_id,
            incident_kind=incident.kind,
            observed_at=observed_at,
            token=token if decision is PublicationDecision.PUBLISH else None,
            recovery_duration_seconds=recovery_duration,
        )

    def complete(self, reservation: PublicationReservation, *, delivered: bool) -> None:
        if reservation.token is None:
            return
        now = self._now()
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT delivered FROM publication_reservations WHERE token = ?",
                (reservation.token,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown publication reservation")
            connection.execute(
                """
                UPDATE publication_reservations
                SET delivered = ?, completed_at = ?
                WHERE token = ?
                """,
                (
                    int(delivered),
                    _iso(now) if delivered else None,
                    reservation.token,
                ),
            )
            if delivered:
                previous = connection.execute(
                    "SELECT outage_started_at FROM monitor_state WHERE monitor_id = ?",
                    (reservation.monitor_id,),
                ).fetchone()
                outage_started_at = _next_outage_start(
                    previous,
                    reservation.incident_kind,
                    reservation.observed_at,
                )
                connection.execute(
                    """
                    INSERT INTO monitor_state(
                        monitor_id, incident_kind, observed_at, outage_started_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(monitor_id) DO UPDATE SET
                        incident_kind=excluded.incident_kind,
                        observed_at=excluded.observed_at,
                        outage_started_at=excluded.outage_started_at,
                        updated_at=excluded.updated_at
                    WHERE excluded.observed_at >= monitor_state.observed_at
                    """,
                    (
                        reservation.monitor_id,
                        reservation.incident_kind.value,
                        _iso(reservation.observed_at),
                        _iso(outage_started_at) if outage_started_at else None,
                        _iso(now),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def prune(self) -> int:
        cutoff = self._now() - timedelta(days=self._retention_days)
        cursor = self._connection.execute(
            """
            DELETE FROM publication_reservations
            WHERE (delivered = 1 OR completed_at IS NOT NULL) AND created_at < ?
            """,
            (_iso(cutoff),),
        )
        return cursor.rowcount

    def close(self) -> None:
        self._connection.close()


def _decision(
    state: sqlite3.Row | None,
    kind: IncidentKind,
    observed_at: datetime,
) -> PublicationDecision:
    if state is None:
        return PublicationDecision.PUBLISH
    previous_at = datetime.fromisoformat(state["observed_at"])
    previous_kind = IncidentKind(state["incident_kind"])
    if observed_at < previous_at:
        return PublicationDecision.STALE
    if observed_at == previous_at:
        return PublicationDecision.STALE if previous_kind is kind else PublicationDecision.PUBLISH
    return PublicationDecision.DUPLICATE if previous_kind is kind else PublicationDecision.PUBLISH


def _recovery_duration(
    state: sqlite3.Row | None,
    kind: IncidentKind,
    observed_at: datetime,
) -> int | None:
    if state is None or kind is not IncidentKind.RECOVERED or not state["outage_started_at"]:
        return None
    started_at = datetime.fromisoformat(state["outage_started_at"])
    return max(0, int((observed_at - started_at).total_seconds()))


def _next_outage_start(
    previous: sqlite3.Row | None,
    kind: IncidentKind,
    observed_at: datetime,
) -> datetime | None:
    if kind is IncidentKind.RECOVERED:
        return None
    if previous is not None and previous["outage_started_at"]:
        return datetime.fromisoformat(previous["outage_started_at"])
    return observed_at


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")
