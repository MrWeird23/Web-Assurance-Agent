from typing import Any

import httpx

from triage_agent.api import create_app


class StubEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def handle(self, payload: dict[str, Any]) -> None:
        self.calls.append(payload)


class InvalidPayloadEngine:
    async def handle(self, payload: dict[str, Any]) -> None:
        del payload
        raise ValueError("Missing monitor URL")


async def test_root_identifies_healthy_service() -> None:
    app = create_app(engine=StubEngine(), webhook_token="expected-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "Web Assurance Agent",
        "status": "ok",
    }


def test_api_advertises_current_package_version() -> None:
    app = create_app(engine=StubEngine(), webhook_token="expected-secret")

    assert app.version == "0.2.0"


async def test_webhook_rejects_missing_authentication_token() -> None:
    engine = StubEngine()
    app = create_app(engine=engine, webhook_token="expected-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/webhooks/uptime-kuma", json={})

    assert response.status_code == 401
    assert engine.calls == []


async def test_webhook_rejects_incorrect_token_before_large_body_processing() -> None:
    engine = StubEngine()
    app = create_app(engine=engine, webhook_token="expected-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/webhooks/uptime-kuma",
            headers={"X-Triage-Token": "incorrect-secret"},
            content=b"x" * 70_000,
        )

    assert response.status_code == 401
    assert engine.calls == []


async def test_webhook_rejects_authenticated_oversized_body() -> None:
    engine = StubEngine()
    app = create_app(engine=engine, webhook_token="expected-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/webhooks/uptime-kuma",
            headers={"X-Triage-Token": "expected-secret"},
            content=b"x" * 70_000,
        )

    assert response.status_code == 413
    assert engine.calls == []


async def test_webhook_rejects_malformed_or_non_object_json() -> None:
    engine = StubEngine()
    app = create_app(engine=engine, webhook_token="expected-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        malformed = await client.post(
            "/webhooks/uptime-kuma",
            headers={"X-Triage-Token": "expected-secret"},
            content=b"{not-json",
        )
        non_object = await client.post(
            "/webhooks/uptime-kuma",
            headers={"X-Triage-Token": "expected-secret"},
            json=["not", "an", "object"],
        )

    assert malformed.status_code == 400
    assert non_object.status_code == 400
    assert engine.calls == []


async def test_webhook_dispatches_authenticated_json_object() -> None:
    engine = StubEngine()
    app = create_app(engine=engine, webhook_token="expected-secret")
    payload: dict[str, Any] = {"heartbeat": {}, "monitor": {}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/webhooks/uptime-kuma",
            headers={"X-Triage-Token": "expected-secret"},
            json=payload,
        )

    assert response.status_code == 202
    assert engine.calls == [payload]


async def test_webhook_returns_bad_request_for_invalid_kuma_payload() -> None:
    app = create_app(engine=InvalidPayloadEngine(), webhook_token="expected-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/webhooks/uptime-kuma",
            headers={"X-Triage-Token": "expected-secret"},
            json={},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Missing monitor URL"}
