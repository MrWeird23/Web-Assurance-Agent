from triage_agent.classification import Incident, IncidentKind
from triage_agent.events import EventState, KumaEvent
from triage_agent.probes import ProbeResult
from triage_agent.reporting import render_discord_payload


def test_renders_confirmed_outage_as_structured_discord_embed() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Client Shop",
        url="https://shop.example.com/",
        state=EventState.DOWN,
        error="Request failed with status code 503",
        observed_at="2026-08-04 13:20:00",
    )
    incident = Incident(
        kind=IncidentKind.CONFIRMED_OUTAGE,
        confirmed=True,
        summary="Independent confirmation attempts failed with HTTP 503.",
        recommendation="Inspect the application or origin service immediately.",
    )
    probes = [
        ProbeResult(
            ok=False,
            status_code=503,
            latency_ms=180,
            final_url=event.url,
            error="HTTP 503",
            server="cloudflare",
            cloudflare_ray="abc123-LIS",
        )
    ]

    payload = render_discord_payload(event, incident, probes)

    assert payload["username"] == "Web Assurance Agent"
    embed = payload["embeds"][0]
    assert embed["title"] == "Confirmed outage: Client Shop"
    assert embed["color"] == 0xD83C3E
    assert embed["url"] == event.url
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Kuma evidence"] == "Request failed with status code 503"
    assert "HTTP 503" in fields["Independent confirmation"]
    assert "CF-Ray: abc123-LIS" in fields["Independent confirmation"]


def test_report_removes_credentials_and_query_secrets_from_target_url() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Client Shop",
        url="https://user:password@example.com/status?token=secret#private",
        state=EventState.UP,
        error="200 OK",
        observed_at="2026-08-04 13:20:00",
    )
    incident = Incident(
        kind=IncidentKind.RECOVERED,
        confirmed=True,
        summary="Recovered",
        recommendation="No action",
    )

    payload = render_discord_payload(event, incident, [])

    assert payload["embeds"][0]["url"] == "https://example.com/status"
    assert "secret" not in str(payload)


def test_report_redacts_secrets_from_kuma_evidence() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Client Shop",
        url="https://shop.example.com/",
        state=EventState.DOWN,
        error=(
            "GET https://shop.example.com/status?token=QUERY_SENTINEL failed; "
            "api_key=PLAIN_SENTINEL"
        ),
        observed_at="2026-08-04 13:20:00",
    )
    incident = Incident(
        kind=IncidentKind.TRANSIENT_FAILURE,
        confirmed=False,
        summary="Unconfirmed",
        recommendation="Continue monitoring",
    )
    probes = [
        ProbeResult(
            ok=False,
            status_code=None,
            latency_ms=10,
            final_url=event.url,
            error=(
                "ConnectError: https://shop.example.com/status?password=PROBE_SENTINEL"
            ),
            server=None,
        )
    ]

    payload = render_discord_payload(event, incident, probes)

    evidence = payload["embeds"][0]["fields"][0]["value"]
    assert evidence == (
        "GET https://shop.example.com/status failed; api_key=[REDACTED]"
    )
    assert "QUERY_SENTINEL" not in str(payload)
    assert "PLAIN_SENTINEL" not in str(payload)
    assert "PROBE_SENTINEL" not in str(payload)
