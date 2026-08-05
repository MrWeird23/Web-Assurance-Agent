from typing import Any

import httpx
import pytest

from triage_agent.discord import DiscordPublisher, DiscordPublishError


async def test_discord_publisher_posts_payload_to_configured_webhook() -> None:
    received: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(__import__("json").loads(request.content))
        return httpx.Response(204)

    payload = {"username": "Web Assurance Agent", "embeds": [{"title": "Incident"}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = DiscordPublisher(
            webhook_url="https://discord.com/api/webhooks/test/value",
            client=client,
        )
        await publisher(payload)

    assert received == [payload]


async def test_discord_publisher_redacts_webhook_secret_from_delivery_errors() -> None:
    webhook_url = "https://discord.com/api/webhooks/123/super-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = DiscordPublisher(webhook_url=webhook_url, client=client)
        with pytest.raises(DiscordPublishError) as captured:
            await publisher({"embeds": []})

    assert str(captured.value) == "Discord webhook delivery failed"
    assert captured.value.__cause__ is None
    assert "super-secret-token" not in repr(captured.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/token",
        "https://example.com/api/webhooks/123/token",
        "https://user:password@discord.com/api/webhooks/123/token",
        "https://discord.com:8443/api/webhooks/123/token",
    ],
)
def test_discord_publisher_rejects_unsafe_webhook_urls(url: str) -> None:
    with pytest.raises(ValueError, match="Invalid Discord webhook URL"):
        DiscordPublisher(webhook_url=url, client=httpx.AsyncClient())
