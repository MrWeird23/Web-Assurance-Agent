"""Validated, atomic persistence for admin-editable runtime settings.

Mirrors ManifestStore's atomic-write pattern. Fields here are the ones an
operator may reasonably want to tune without touching source/.env: Discord
webhook URL, confirmation/timeout tuning, concurrency limits, log level.
TRIAGE_WEBHOOK_TOKEN and TRIAGE_ALLOWED_HOSTS stay .env-only -- exposing them
here would let anyone with admin access change their own security boundary.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml

from triage_agent.discord import validate_discord_webhook_url

LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
MAX_SETTINGS_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    discord_webhook_url: str | None = None
    confirmation_attempts: int = 2
    confirmation_delay_seconds: float = 5
    request_timeout_seconds: float = 15
    manual_check_concurrency: int = 1
    scheduler_global_concurrency: int = 2
    scheduler_site_concurrency: int = 1
    log_level: str = "INFO"

    # Fields whose new value only takes effect after a process restart
    # (baked into an httpx client / asyncio.Queue at construction time).
    COLD_FIELDS: ClassVar[tuple[str, ...]] = (
        "request_timeout_seconds",
        "manual_check_concurrency",
    )

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> RuntimeSettings:
        discord_url = str(values.get("discord_webhook_url") or "").strip() or None
        if discord_url is not None:
            try:
                validate_discord_webhook_url(discord_url)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc

        attempts = int(values.get("confirmation_attempts", 2))
        if not 1 <= attempts <= 5:
            raise ValueError("confirmation_attempts must be between 1 and 5")

        delay = float(values.get("confirmation_delay_seconds", 5))
        if not 0 <= delay <= 60:
            raise ValueError("confirmation_delay_seconds must be between 0 and 60")

        timeout = float(values.get("request_timeout_seconds", 15))
        if not 1 <= timeout <= 60:
            raise ValueError("request_timeout_seconds must be between 1 and 60")

        manual_check_concurrency = int(values.get("manual_check_concurrency", 1))
        if not 1 <= manual_check_concurrency <= 4:
            raise ValueError("manual_check_concurrency must be between 1 and 4")

        scheduler_global_concurrency = int(values.get("scheduler_global_concurrency", 2))
        if not 1 <= scheduler_global_concurrency <= 8:
            raise ValueError("scheduler_global_concurrency must be between 1 and 8")

        scheduler_site_concurrency = int(values.get("scheduler_site_concurrency", 1))
        if not 1 <= scheduler_site_concurrency <= 4:
            raise ValueError("scheduler_site_concurrency must be between 1 and 4")

        log_level = str(values.get("log_level", "INFO")).strip().upper() or "INFO"
        if log_level not in LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(LOG_LEVELS)}")

        return cls(
            discord_webhook_url=discord_url,
            confirmation_attempts=attempts,
            confirmation_delay_seconds=delay,
            request_timeout_seconds=timeout,
            manual_check_concurrency=manual_check_concurrency,
            scheduler_global_concurrency=scheduler_global_concurrency,
            scheduler_site_concurrency=scheduler_site_concurrency,
            log_level=log_level,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "discord_webhook_url": self.discord_webhook_url,
            "confirmation_attempts": self.confirmation_attempts,
            "confirmation_delay_seconds": self.confirmation_delay_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "manual_check_concurrency": self.manual_check_concurrency,
            "scheduler_global_concurrency": self.scheduler_global_concurrency,
            "scheduler_site_concurrency": self.scheduler_site_concurrency,
            "log_level": self.log_level,
        }

    def restart_required_vs(self, previous: RuntimeSettings) -> bool:
        return any(
            getattr(self, name) != getattr(previous, name) for name in self.COLD_FIELDS
        )


class SettingsStore:
    def __init__(self, path: Path, *, defaults: RuntimeSettings) -> None:
        self.path = path
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError("settings.yaml must contain a mapping")
            self.settings = RuntimeSettings.from_dict(raw)
        else:
            self.settings = defaults

    def replace(self, payload: dict[str, Any]) -> RuntimeSettings:
        settings = RuntimeSettings.from_dict(payload)
        rendered = yaml.safe_dump(
            settings.as_dict(), allow_unicode=True, default_flow_style=False, sort_keys=False
        )
        if len(rendered.encode("utf-8")) > MAX_SETTINGS_BYTES:
            raise ValueError("Settings payload is too large")
        self._atomic_write(rendered)
        self.settings = settings
        return settings

    def _atomic_write(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
