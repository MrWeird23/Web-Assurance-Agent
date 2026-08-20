from triage_agent.wordpress_health import WordPressHealthResult
from triage_agent.wordpress_reporting import (
    render_wordpress_health_discord_payload,
    wordpress_health_failure_codes,
)


def test_healthy_wordpress_result_has_no_alert_codes() -> None:
    result = WordPressHealthResult(
        ok=True,
        error_code=None,
        core_version="6.8.1",
        core_update_available=False,
        plugin_updates=(),
        theme_update_available=False,
        site_health_status="good",
        critical_test_count=0,
        overdue_cron_count=0,
        failing_cron_count=0,
        rest_api_ok=True,
        fatal_error_codes=(),
    )

    assert wordpress_health_failure_codes(result) == ()


def test_critical_and_fatal_health_produce_stable_deduplicated_codes() -> None:
    result = WordPressHealthResult(
        ok=True,
        error_code=None,
        core_version="6.8.1",
        core_update_available=True,
        plugin_updates=("contact-form-7",),
        theme_update_available=True,
        site_health_status="critical",
        critical_test_count=2,
        overdue_cron_count=3,
        failing_cron_count=1,
        rest_api_ok=False,
        fatal_error_codes=("php_fatal_error", "php_fatal_error"),
    )

    assert wordpress_health_failure_codes(result) == (
        "wordpress_core_update_available",
        "wordpress_cron_failing",
        "wordpress_cron_overdue",
        "wordpress_fatal_error",
        "wordpress_rest_api_failure",
        "wordpress_site_health_critical",
        "wordpress_theme_update_available",
    )


def test_rendered_wordpress_alert_is_bounded_and_excludes_sensitive_values() -> None:
    result = WordPressHealthResult(
        ok=True,
        error_code=None,
        core_version="6.8.1",
        core_update_available=False,
        plugin_updates=("contact-form-7", "woocommerce"),
        theme_update_available=False,
        site_health_status="critical",
        critical_test_count=2,
        overdue_cron_count=0,
        failing_cron_count=1,
        rest_api_ok=False,
        fatal_error_codes=("php_fatal_error",),
    )

    payload = render_wordpress_health_discord_payload(
        page_id="home",
        check_id="site-health",
        result=result,
    )

    assert payload["username"] == "Web Assurance Agent"
    embed = payload["embeds"][0]
    assert embed["title"] == "WordPress administrative health alert"
    assert embed["color"] == 0xD83C3E
    assert "home" in embed["description"]
    rendered = str(payload)
    assert "https://" not in rendered
    assert "token" not in rendered.lower()
    assert "php_fatal_error" not in rendered
    assert len(rendered) < 4000


def test_extreme_typed_values_remain_within_discord_limits() -> None:
    result = WordPressHealthResult(
        ok=True,
        error_code=None,
        core_version="v" * 32,
        plugin_updates=tuple(f"plugin-{index}" for index in range(500)),
        site_health_status="critical",
        critical_test_count=100,
        overdue_cron_count=100_000,
        failing_cron_count=100_000,
        rest_api_ok=False,
        fatal_error_codes=tuple("fatal-code" for _ in range(100)),
    )

    payload = render_wordpress_health_discord_payload(
        page_id="p" * 64,
        check_id="c" * 64,
        result=result,
    )

    embed = payload["embeds"][0]
    assert len(embed["description"]) <= 4096
    assert len(embed["fields"]) <= 25
    assert all(len(field["name"]) <= 256 for field in embed["fields"])
    assert all(len(field["value"]) <= 1024 for field in embed["fields"])
    assert len(str(payload)) < 6000


def test_collection_failure_uses_warning_severity_and_safe_rest_state() -> None:
    result = WordPressHealthResult(ok=False, error_code="missing_secret")

    payload = render_wordpress_health_discord_payload(
        page_id="home",
        check_id="site-health",
        result=result,
    )

    embed = payload["embeds"][0]
    assert embed["color"] == 0xF0B232
    runtime_field = next(
        field for field in embed["fields"] if field["name"] == "Runtime health"
    )
    assert "REST API: unavailable" in runtime_field["value"]
    assert "wordpress_health_missing_secret" in str(payload)
