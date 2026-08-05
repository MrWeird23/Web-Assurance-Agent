from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventState(StrEnum):
    DOWN = "down"
    UP = "up"


@dataclass(frozen=True, slots=True)
class KumaEvent:
    monitor_id: int
    monitor_name: str
    url: str
    state: EventState
    error: str
    observed_at: str

    @property
    def observed_datetime(self) -> datetime:
        return parse_observed_at(self.observed_at)


def parse_observed_at(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid Uptime Kuma observation time")
    try:
        observed_at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid Uptime Kuma observation time") from exc
    if observed_at.tzinfo is None:
        return observed_at.replace(tzinfo=UTC)
    return observed_at.astimezone(UTC)


def parse_kuma_event(payload: dict[str, Any]) -> KumaEvent:
    try:
        heartbeat = payload["heartbeat"]
        monitor = payload["monitor"]
        if not isinstance(heartbeat, dict) or not isinstance(monitor, dict):
            raise ValueError
        status = heartbeat["status"]
        monitor_id = monitor["id"]
        monitor_name = monitor["name"]
        url = monitor["url"]
        observed_at = heartbeat["time"]
        error = heartbeat.get("msg") or ""
        parse_observed_at(observed_at)
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or status not in {0, 1}
            or not isinstance(monitor_id, int)
            or isinstance(monitor_id, bool)
            or monitor_id < 1
            or not isinstance(monitor_name, str)
            or not monitor_name.strip()
            or len(monitor_name) > 200
            or not isinstance(url, str)
            or not url.strip()
            or len(url) > 2048
            or not isinstance(error, str)
            or len(error) > 1000
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid Uptime Kuma payload") from exc
    state = EventState.UP if status == 1 else EventState.DOWN
    return KumaEvent(
        monitor_id=monitor_id,
        monitor_name=monitor_name.strip(),
        url=url.strip(),
        state=state,
        error=error,
        observed_at=observed_at,
    )
