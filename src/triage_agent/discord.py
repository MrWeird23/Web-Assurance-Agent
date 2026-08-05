from typing import Any
from urllib.parse import urlsplit

import httpx


class DiscordPublishError(RuntimeError):
    pass


def validate_discord_webhook_url(webhook_url: str) -> None:
    try:
        parsed = urlsplit(webhook_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid Discord webhook URL") from exc
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"discord.com", "canary.discord.com", "ptb.discord.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or len(path_parts) != 4
        or path_parts[:2] != ["api", "webhooks"]
    ):
        raise ValueError("Invalid Discord webhook URL")


class DiscordPublisher:
    def __init__(self, *, webhook_url: str, client: httpx.AsyncClient) -> None:
        validate_discord_webhook_url(webhook_url)
        self._webhook_url = webhook_url
        self._client = client

    async def __call__(self, payload: dict[str, Any]) -> None:
        try:
            response = await self._client.post(self._webhook_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError:
            raise DiscordPublishError("Discord webhook delivery failed") from None
