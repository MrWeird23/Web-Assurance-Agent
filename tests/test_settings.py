from pathlib import Path

from triage_agent.settings import Settings


def test_settings_parse_safe_runtime_environment() -> None:
    settings = Settings.from_mapping(
        {
            "TRIAGE_WEBHOOK_TOKEN": "a-secure-random-token",
            "TRIAGE_ALLOWED_HOSTS": "monitor.example.com, www.monitor.example.com",
            "TRIAGE_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test/value",
            "TRIAGE_CONFIRMATION_ATTEMPTS": "3",
            "TRIAGE_CONFIRMATION_DELAY_SECONDS": "5",
            "TRIAGE_REQUEST_TIMEOUT_SECONDS": "10",
            "TRIAGE_SITE_MANIFEST_PATH": "/run/config/sites.yaml",
            "TRIAGE_BROWSER_ARTIFACT_DIRECTORY": "/tmp/browser-artifacts",
            "TRIAGE_MANUAL_CHECK_CONCURRENCY": "2",
        }
    )

    assert settings.webhook_token == "a-secure-random-token"
    assert settings.allowed_hosts == frozenset({"monitor.example.com", "www.monitor.example.com"})
    assert settings.confirmation_attempts == 3
    assert settings.confirmation_delay_seconds == 5.0
    assert settings.request_timeout_seconds == 10.0
    assert settings.site_manifest_path == Path("/run/config/sites.yaml")
    assert settings.browser_artifact_directory == Path("/tmp/browser-artifacts")
    assert settings.manual_check_concurrency == 2


def test_settings_leave_browser_checks_disabled_without_manifest_path() -> None:
    settings = Settings.from_mapping(
        {
            "TRIAGE_WEBHOOK_TOKEN": "a-secure-random-token",
            "TRIAGE_ALLOWED_HOSTS": "monitor.example.com",
        }
    )

    assert settings.site_manifest_path is None
    assert settings.browser_artifact_directory is None
    assert settings.manual_check_concurrency == 1


def test_settings_reject_invalid_manual_check_concurrency() -> None:
    values = {
        "TRIAGE_WEBHOOK_TOKEN": "a-secure-random-token",
        "TRIAGE_ALLOWED_HOSTS": "monitor.example.com",
        "TRIAGE_MANUAL_CHECK_CONCURRENCY": "0",
    }

    try:
        Settings.from_mapping(values)
    except ValueError as error:
        assert str(error) == "TRIAGE_MANUAL_CHECK_CONCURRENCY must be between 1 and 4"
    else:
        raise AssertionError("Expected invalid concurrency to be rejected")
