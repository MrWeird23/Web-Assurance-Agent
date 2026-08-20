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
    assert embed["title"] == "Confirmed outage"
    assert embed["color"] == 0xD83C3E
    assert embed["url"] == event.url
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Kuma evidence"] == "Kuma reported a failure."
    assert "HTTP 503" in fields["Independent confirmation"]
    assert "CF-Ray header present" in fields["Independent confirmation"]
    assert "abc123-LIS" not in fields["Independent confirmation"]


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

    assert payload["embeds"][0]["url"] == "https://example.com/"
    assert "secret" not in str(payload)


def test_report_omits_untrusted_kuma_and_probe_evidence() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Client Shop",
        url="https://shop.example.com/",
        state=EventState.DOWN,
        error=(
            "GET https://shop.example.com/PATH_SENTINEL?token=QUERY_SENTINEL failed; "
            "api_key=PLAIN_SENTINEL; token: COLON_SENTINEL"
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
                "ConnectError: https://shop.example.com/PROBE_PATH_SENTINEL"
            ),
            server=None,
        )
    ]

    payload = render_discord_payload(event, incident, probes)

    evidence = payload["embeds"][0]["fields"][0]["value"]
    assert evidence == "Kuma reported a failure."
    assert "PATH_SENTINEL" not in str(payload)
    assert "QUERY_SENTINEL" not in str(payload)
    assert "PLAIN_SENTINEL" not in str(payload)
    assert "COLON_SENTINEL" not in str(payload)
    assert "PROBE_PATH_SENTINEL" not in str(payload)


def test_report_does_not_disclose_target_paths_or_response_header_values() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Client Shop",
        url="https://example.com/PATH_SENTINEL?token=QUERY_SENTINEL",
        state=EventState.DOWN,
        error="Request failed with status code 503",
        observed_at="2026-08-04T13:20:00+00:00",
    )
    incident = Incident(
        kind=IncidentKind.CONFIRMED_OUTAGE,
        confirmed=True,
        summary="Confirmed",
        recommendation="Inspect the origin",
    )
    probes = [
        ProbeResult(
            ok=False,
            status_code=503,
            latency_ms=180,
            final_url=event.url,
            error="HTTP 503",
            server="SERVER_SENTINEL",
            cloudflare_ray="RAY_SENTINEL",
        )
    ]

    payload = render_discord_payload(event, incident, probes)
    serialized = str(payload)

    assert payload["embeds"][0]["url"] == "https://example.com/"
    assert "PATH_SENTINEL" not in serialized
    assert "QUERY_SENTINEL" not in serialized
    assert "SERVER_SENTINEL" not in serialized
    assert "RAY_SENTINEL" not in serialized


def test_report_does_not_disclose_monitor_or_incident_text() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="MONITOR_NAME_SENTINEL",
        url="https://example.com/",
        state=EventState.DOWN,
        error="HTTP 503",
        observed_at="2026-08-04T13:20:00+00:00",
    )
    incident = Incident(
        kind=IncidentKind.CONFIRMED_OUTAGE,
        confirmed=True,
        summary="INCIDENT_SUMMARY_SENTINEL",
        recommendation="INCIDENT_RECOMMENDATION_SENTINEL",
    )

    serialized = str(render_discord_payload(event, incident, []))

    assert "MONITOR_NAME_SENTINEL" not in serialized
    assert "INCIDENT_SUMMARY_SENTINEL" not in serialized
    assert "INCIDENT_RECOMMENDATION_SENTINEL" not in serialized


def test_report_does_not_disclose_free_text_kuma_or_probe_errors() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Example",
        url="https://example.com/",
        state=EventState.DOWN,
        error="Authorization: Bearer BEARER_SENTINEL; Cookie: COOKIE_SENTINEL",
        observed_at="2026-08-04T13:20:00+00:00",
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
            error="PROBE_ERROR_SENTINEL",
            server=None,
        )
    ]

    serialized = str(render_discord_payload(event, incident, probes))

    assert "BEARER_SENTINEL" not in serialized
    assert "COOKIE_SENTINEL" not in serialized
    assert "PROBE_ERROR_SENTINEL" not in serialized


def test_report_includes_recovery_duration_when_known() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Client Shop",
        url="https://example.com/",
        state=EventState.UP,
        error="",
        observed_at="2026-08-04T13:20:00+00:00",
    )
    incident = Incident(
        kind=IncidentKind.RECOVERED,
        confirmed=True,
        summary="Recovered",
        recommendation="No action",
    )

    payload = render_discord_payload(event, incident, [], recovery_duration_seconds=3725)

    fields = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}
    assert fields["Recovery duration"] == "1h 2m 5s"


def test_report_omits_recovery_duration_field_when_unknown() -> None:
    event = KumaEvent(
        monitor_id=11,
        monitor_name="Client Shop",
        url="https://example.com/",
        state=EventState.DOWN,
        error="HTTP 503",
        observed_at="2026-08-04T13:20:00+00:00",
    )
    incident = Incident(
        kind=IncidentKind.CONFIRMED_OUTAGE,
        confirmed=True,
        summary="Confirmed",
        recommendation="Inspect the origin",
    )

    payload = render_discord_payload(event, incident, [])

    names = [field["name"] for field in payload["embeds"][0]["fields"]]
    assert "Recovery duration" not in names
