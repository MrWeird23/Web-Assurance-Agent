import json
from pathlib import Path

import httpx

from triage_agent.api import create_app
from triage_agent.manifest_store import ManifestStore

MANIFEST = """\
version: 1
sites:
  - id: example
    allowed_hosts: [example.com]
    pages:
      - id: home
        url: https://example.com/
        viewports:
          - {id: desktop, width: 1440, height: 900, device_scale_factor: 1.0}
"""


class StubEngine:
    async def handle(self, payload: dict[str, object]) -> None:
        del payload


def _manifest_payload(*, host: str = "example.com") -> dict[str, object]:
    return {
        "version": 1,
        "sites": [
            {
                "id": "example",
                "allowed_hosts": [host],
                "pages": [
                    {
                        "id": "home",
                        "url": f"https://{host}/",
                        "viewports": [
                            {
                                "id": "desktop",
                                "width": 1440,
                                "height": 900,
                                "device_scale_factor": 1.0,
                            }
                        ],
                    }
                ],
            }
        ],
    }


async def test_admin_shell_is_available_without_exposing_manifest(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Site configuration" in response.text
    assert "required_text: [Welcome]" not in response.text
    assert "Test link" in response.text
    assert "Run tests" in response.text
    assert "Run full triage" in response.text
    assert "/admin/api/run-full-triage" in response.text
    assert "Reference screenshots" in response.text
    assert "Capture reference" in response.text
    assert "Approve reference" in response.text
    assert 'id="baselinePreview"' in response.text
    assert 'role="dialog"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'id="crawlEnabled"' in response.text
    assert 'id="crawlMaxPages"' in response.text
    assert 'id="crawlMaxDepth"' in response.text
    assert 'id="crawlExclude"' in response.text
    assert "e.event==='crawl_start'" in response.text
    assert "e.event==='crawl_page'" in response.text
    assert "e.event==='crawl_result'" in response.text


async def test_admin_manifest_api_requires_authentication(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/admin/api/manifest")

    assert response.status_code == 401


async def test_admin_can_read_and_atomically_replace_manifest(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    store = ManifestStore(path, permitted_hosts={"example.com"})
    app = create_app(
        engine=StubEngine(), webhook_token="expected-secret", manifest_store=store
    )
    headers = {"Authorization": "Bearer expected-secret"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        before = await client.get("/admin/api/manifest", headers=headers)
        saved = await client.put(
            "/admin/api/manifest", headers=headers, json=_manifest_payload()
        )
        after = await client.get("/admin/api/manifest", headers=headers)

    assert before.status_code == 200
    assert saved.status_code == 200
    assert saved.json() == {"status": "saved", "restart_required": True}
    assert after.json()["sites"][0]["pages"][0]["url"] == "https://example.com/"
    assert json.loads(json.dumps(store.registry.manifest.model_dump(mode="json")))["sites"][0][
        "allowed_hosts"
    ] == ["example.com"]
    assert "example.com" in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


async def test_admin_rejects_hosts_outside_runtime_allowlist_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    original = path.read_bytes()
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.put(
            "/admin/api/manifest",
            headers={"Authorization": "Bearer expected-secret"},
            json=_manifest_payload(host="unapproved.example"),
        )

    assert response.status_code == 422
    assert "TRIAGE_ALLOWED_HOSTS" in response.json()["detail"]
    assert path.read_bytes() == original


async def test_admin_rejects_oversized_payload_before_json_parsing(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.put(
            "/admin/api/manifest",
            headers={
                "Authorization": "Bearer expected-secret",
                "Content-Type": "application/json",
            },
            content=b"{" + b" " * 1_100_000,
        )

    assert response.status_code == 413
