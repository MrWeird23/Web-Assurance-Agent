import pytest

from triage_agent.events import EventState, parse_kuma_event


def test_parses_kuma_down_event() -> None:
    payload = {
        "heartbeat": {
            "status": 0,
            "time": "2026-08-04 13:14:37",
            "msg": "Request failed with status code 403",
            "ping": None,
        },
        "monitor": {
            "id": 10,
            "name": "Example Site",
            "type": "http",
            "url": "https://monitor.example.com/",
        },
        "msg": "Example Site is down",
    }

    event = parse_kuma_event(payload)

    assert event.monitor_id == 10
    assert event.monitor_name == "Example Site"
    assert event.url == "https://monitor.example.com/"
    assert event.state is EventState.DOWN
    assert event.error == "Request failed with status code 403"
    assert event.observed_at == "2026-08-04 13:14:37"


def test_rejects_payload_without_required_monitor_fields() -> None:
    with pytest.raises(ValueError, match="Invalid Uptime Kuma payload"):
        parse_kuma_event({"heartbeat": {"status": 0}, "monitor": {}})


@pytest.mark.parametrize("status", [True, False, 0.5, "1"])
def test_rejects_coerced_status_values(status: object) -> None:
    with pytest.raises(ValueError, match="Invalid Uptime Kuma payload"):
        parse_kuma_event(
            {
                "heartbeat": {"status": status, "time": "2026-08-04 13:14:37"},
                "monitor": {"id": 10, "name": "Site", "url": "https://example.com/"},
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", True),
        ("name", "   "),
        ("name", "x" * 201),
        ("url", "x" * 2049),
    ],
)
def test_rejects_invalid_monitor_scalars(field: str, value: object) -> None:
    monitor: dict[str, object] = {
        "id": 10,
        "name": "Site",
        "url": "https://example.com/",
    }
    monitor[field] = value
    with pytest.raises(ValueError, match="Invalid Uptime Kuma payload"):
        parse_kuma_event(
            {
                "heartbeat": {"status": 0, "time": "2026-08-04 13:14:37"},
                "monitor": monitor,
            }
        )


def test_rejects_invalid_observation_time() -> None:
    with pytest.raises(ValueError, match="Invalid Uptime Kuma payload"):
        parse_kuma_event(
            {
                "heartbeat": {"status": 0, "time": "not-a-time"},
                "monitor": {"id": 10, "name": "Site", "url": "https://example.com/"},
            }
        )
