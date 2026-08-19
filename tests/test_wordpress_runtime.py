import httpx

from triage_agent.wordpress_health import WordPressHealthResult
from triage_agent.wordpress_runtime import EnvironmentSecretLoader, WordPressRuntimeChecker


async def public_resolver(_host: str) -> set[str]:
    return {"93.184.216.34"}


def test_environment_secret_loader_uses_prefixed_normalized_reference() -> None:
    loader = EnvironmentSecretLoader(
        {"TRIAGE_SECRET_EXAMPLE_SITE_TOKEN": "site-specific-secret"}
    )

    assert loader.get("example", "site-token") == "site-specific-secret"


def test_environment_secret_loader_rejects_missing_or_short_secret() -> None:
    loader = EnvironmentSecretLoader({"TRIAGE_SECRET_EXAMPLE_SITE_TOKEN": "short"})

    assert loader.get("example", "site-token") is None
    assert loader.get("example", "missing") is None


async def test_runtime_checker_fails_closed_without_secret() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Network must not be reached without a secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        checker = WordPressRuntimeChecker(
            client=client,
            resolver=public_resolver,
            secrets=EnvironmentSecretLoader({}),
        )
        result = await checker.run(
            endpoint="https://example.com/wp-json/techx-monitor/v1/health",
            site_id="example",
            token_secret_ref="site-token",
            allowed_hosts={"example.com"},
        )

    assert result == WordPressHealthResult(ok=False, error_code="missing_secret")
