"""Runtime secret resolution for authorized WordPress health checks."""

import re
from collections.abc import Awaitable, Callable, Mapping

import httpx

from triage_agent.wordpress_health import WordPressHealthResult, fetch_wordpress_health

_SECRET_REF = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class EnvironmentSecretLoader:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values

    def get(self, site_id: str, reference: str) -> str | None:
        if _SECRET_REF.fullmatch(site_id) is None or _SECRET_REF.fullmatch(reference) is None:
            return None
        normalized_site = site_id.replace("-", "_").upper()
        normalized_reference = reference.replace("-", "_").upper()
        key = f"TRIAGE_SECRET_{normalized_site}_{normalized_reference}"
        value = self._values.get(key, "").strip()
        return value if len(value) >= 16 else None


class WordPressRuntimeChecker:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        resolver: Callable[[str], Awaitable[set[str]]],
        secrets: EnvironmentSecretLoader,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._secrets = secrets

    async def run(
        self,
        *,
        endpoint: str,
        site_id: str,
        token_secret_ref: str,
        allowed_hosts: set[str],
    ) -> WordPressHealthResult:
        token = self._secrets.get(site_id, token_secret_ref)
        if token is None:
            return WordPressHealthResult(ok=False, error_code="missing_secret")
        return await fetch_wordpress_health(
            url=endpoint,
            allowed_hosts=allowed_hosts,
            token=token,
            client=self._client,
            resolver=self._resolver,
        )
