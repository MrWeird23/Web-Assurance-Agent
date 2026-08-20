from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    webhook_token: str
    allowed_hosts: frozenset[str]
    discord_webhook_url: str | None
    confirmation_attempts: int
    confirmation_delay_seconds: float
    request_timeout_seconds: float
    site_manifest_path: Path | None = None
    browser_artifact_directory: Path | None = None
    manual_check_concurrency: int = 1

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
        manual_check_concurrency = int(values.get("TRIAGE_MANUAL_CHECK_CONCURRENCY", "1"))
        if not 1 <= manual_check_concurrency <= 4:
            raise ValueError("TRIAGE_MANUAL_CHECK_CONCURRENCY must be between 1 and 4")
        discord_url = values.get("TRIAGE_DISCORD_WEBHOOK_URL", "").strip() or None
        manifest_path = values.get("TRIAGE_SITE_MANIFEST_PATH", "").strip()
        artifact_directory = values.get("TRIAGE_BROWSER_ARTIFACT_DIRECTORY", "").strip()
        return cls(
            webhook_token=token,
            allowed_hosts=hosts,
            discord_webhook_url=discord_url,
            confirmation_attempts=attempts,
            confirmation_delay_seconds=delay,
            request_timeout_seconds=timeout,
            site_manifest_path=Path(manifest_path) if manifest_path else None,
            browser_artifact_directory=Path(artifact_directory) if artifact_directory else None,
            manual_check_concurrency=manual_check_concurrency,
        )
