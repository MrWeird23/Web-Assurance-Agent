"""Credential-safe WordPress administrative health alert formatting."""

from typing import Any

from triage_agent.wordpress_health import WordPressHealthResult


def _rest_api_status(value: bool | None) -> str:
    if value is None:
        return "unavailable"
    return "healthy" if value else "failed"


def wordpress_health_failure_codes(result: WordPressHealthResult) -> tuple[str, ...]:
    codes: set[str] = set()
    if not result.ok:
        codes.add(f"wordpress_health_{result.error_code or 'unavailable'}")
    if result.core_update_available:
        codes.add("wordpress_core_update_available")
    if result.theme_update_available:
        codes.add("wordpress_theme_update_available")
    if result.site_health_status == "critical":
        codes.add("wordpress_site_health_critical")
    if result.overdue_cron_count:
        codes.add("wordpress_cron_overdue")
    if result.failing_cron_count:
        codes.add("wordpress_cron_failing")
    if result.rest_api_ok is False:
        codes.add("wordpress_rest_api_failure")
    if result.fatal_error_codes:
        codes.add("wordpress_fatal_error")
    return tuple(sorted(codes))


def render_wordpress_health_discord_payload(
    *,
    page_id: str,
    check_id: str,
    result: WordPressHealthResult,
) -> dict[str, Any]:
    """Render bounded typed evidence without endpoints, credentials, or raw errors."""
    failure_codes = wordpress_health_failure_codes(result)
    critical = (
        "wordpress_site_health_critical" in failure_codes
        or "wordpress_fatal_error" in failure_codes
    )
    fields = [
        {
            "name": "Health check",
            "value": f"Page `{page_id}` · Check `{check_id}`",
            "inline": False,
        },
        {
            "name": "Status",
            "value": result.site_health_status or "unavailable",
            "inline": True,
        },
        {
            "name": "Core version",
            "value": result.core_version or "unavailable",
            "inline": True,
        },
        {
            "name": "Pending updates",
            "value": (
                f"Core: {bool(result.core_update_available)} · "
                f"Theme: {bool(result.theme_update_available)} · "
                f"Plugins: {len(result.plugin_updates)}"
            ),
            "inline": False,
        },
        {
            "name": "Runtime health",
            "value": (
                f"Critical tests: {result.critical_test_count or 0} · "
                f"Overdue cron: {result.overdue_cron_count or 0} · "
                f"Failing cron: {result.failing_cron_count or 0} · "
                f"REST API: {_rest_api_status(result.rest_api_ok)}"
            ),
            "inline": False,
        },
        {
            "name": "Failure codes",
            "value": ", ".join(failure_codes) or "none",
            "inline": False,
        },
    ]
    return {
        "username": "Web Assurance Agent",
        "embeds": [
            {
                "title": "WordPress administrative health alert",
                "color": 0xD83C3E if critical else 0xF0B232,
                "description": f"Read-only administrative health evidence for `{page_id}`.",
                "fields": fields,
            }
        ],
    }
