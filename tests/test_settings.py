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
        }
    )

    assert settings.webhook_token == "a-secure-random-token"
    assert settings.allowed_hosts == frozenset({"monitor.example.com", "www.monitor.example.com"})
    assert settings.confirmation_attempts == 3
    assert settings.confirmation_delay_seconds == 5.0
    assert settings.request_timeout_seconds == 10.0
