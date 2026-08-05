from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    webhook_token: str
    allowed_hosts: frozenset[str]
    discord_webhook_url: str | None
    confirmation_attempts: int
    confirmation_delay_seconds: float
    request_timeout_seconds: float

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "Settings":
        token = values.get("TRIAGE_WEBHOOK_TOKEN", "").strip()
        hosts = frozenset(
            host.strip().lower()
            for host in values.get("TRIAGE_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        if len(token) < 16:
            raise ValueError("TRIAGE_WEBHOOK_TOKEN must contain at least 16 characters")
        if not hosts:
            raise ValueError("TRIAGE_ALLOWED_HOSTS must contain at least one hostname")
        attempts = int(values.get("TRIAGE_CONFIRMATION_ATTEMPTS", "2"))
        if not 1 <= attempts <= 5:
            raise ValueError("TRIAGE_CONFIRMATION_ATTEMPTS must be between 1 and 5")
        delay = float(values.get("TRIAGE_CONFIRMATION_DELAY_SECONDS", "5"))
        if not 0 <= delay <= 60:
            raise ValueError("TRIAGE_CONFIRMATION_DELAY_SECONDS must be between 0 and 60")
        timeout = float(values.get("TRIAGE_REQUEST_TIMEOUT_SECONDS", "15"))
        if not 1 <= timeout <= 60:
            raise ValueError("TRIAGE_REQUEST_TIMEOUT_SECONDS must be between 1 and 60")
        discord_url = values.get("TRIAGE_DISCORD_WEBHOOK_URL", "").strip() or None
        return cls(
            webhook_token=token,
            allowed_hosts=hosts,
            discord_webhook_url=discord_url,
            confirmation_attempts=attempts,
            confirmation_delay_seconds=delay,
            request_timeout_seconds=timeout,
        )
