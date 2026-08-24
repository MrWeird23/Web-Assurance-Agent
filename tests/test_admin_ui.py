import json
from pathlib import Path

import httpx

from triage_agent.api import create_app
from triage_agent.auth_store import UserStore
from triage_agent.manifest_store import ManifestStore
from triage_agent.settings_store import RuntimeSettings, SettingsStore

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "correct-horse-battery-staple"


def _user_store(tmp_path: Path) -> UserStore:
    store = UserStore(tmp_path / "users.sqlite3")
    store.create_user(ADMIN_USERNAME, ADMIN_PASSWORD)
    return store


async def _login_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/admin/api/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}

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
    assert 'id="loginScreen"' in response.text
    assert 'id="username"' in response.text
    assert 'id="logout"' in response.text
    assert 'id="openSettings"' in response.text
    assert 'id="settingsModal"' in response.text
    assert 'id="discordWebhook"' in response.text
    assert 'id="logLevel"' in response.text
    assert "/admin/api/settings" in response.text


async def test_admin_manifest_api_requires_authentication(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/admin/api/manifest")

    assert response.status_code == 401


async def test_admin_api_is_unavailable_without_a_user_store(tmp_path: Path) -> None:
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
        response = await client.get(
            "/admin/api/manifest", headers={"Authorization": "Bearer expected-secret"}
        )

    assert response.status_code == 503


async def test_admin_login_rejects_wrong_password(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/admin/api/login", json={"username": ADMIN_USERNAME, "password": "wrong"}
        )

    assert response.status_code == 401


async def test_admin_logout_revokes_the_session(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _login_headers(client)
        await client.post("/admin/api/logout", headers=headers)
        response = await client.get("/admin/api/manifest", headers=headers)

    assert response.status_code == 401


async def test_admin_can_read_and_atomically_replace_manifest(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    store = ManifestStore(path, permitted_hosts={"example.com"})
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=store,
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _login_headers(client)
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


async def test_admin_save_calls_reload_hook_and_skips_restart_notice(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    store = ManifestStore(path, permitted_hosts={"example.com"})
    calls = []

    async def reload_scheduler() -> None:
        calls.append(1)

    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=store,
        admin_reload_scheduler=reload_scheduler,
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _login_headers(client)
        saved = await client.put(
            "/admin/api/manifest", headers=headers, json=_manifest_payload()
        )

    assert saved.status_code == 200
    assert saved.json() == {"status": "saved", "restart_required": False}
    assert calls == [1]


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
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _login_headers(client)
        response = await client.put(
            "/admin/api/manifest",
            headers=headers,
            json=_manifest_payload(host="unapproved.example"),
        )

    assert response.status_code == 422
    assert "TRIAGE_ALLOWED_HOSTS" in response.json()["detail"]
    assert path.read_bytes() == original


async def test_admin_settings_api_requires_authentication(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
        settings_store=SettingsStore(tmp_path / "settings.yaml", defaults=RuntimeSettings()),
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/admin/api/settings")

    assert response.status_code == 401


async def test_admin_can_read_and_replace_settings(tmp_path: Path) -> None:
    manifest_path = tmp_path / "sites.yaml"
    manifest_path.write_text(MANIFEST, encoding="utf-8")
    settings_store = SettingsStore(tmp_path / "settings.yaml", defaults=RuntimeSettings())
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(manifest_path, permitted_hosts={"example.com"}),
        settings_store=settings_store,
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _login_headers(client)
        before = await client.get("/admin/api/settings", headers=headers)
        saved = await client.put(
            "/admin/api/settings",
            headers=headers,
            json={"confirmation_attempts": 4, "log_level": "warning"},
        )
        after = await client.get("/admin/api/settings", headers=headers)

    assert before.json()["confirmation_attempts"] == 2
    assert saved.status_code == 200
    # No apply hook configured -> can't confirm anything applied live.
    assert saved.json() == {"status": "saved", "restart_required": True}
    assert after.json()["confirmation_attempts"] == 4
    assert after.json()["log_level"] == "WARNING"
    assert "confirmation_attempts: 4" in (tmp_path / "settings.yaml").read_text(
        encoding="utf-8"
    )


async def test_admin_settings_save_calls_apply_hook_and_reports_its_restart_verdict(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "sites.yaml"
    manifest_path.write_text(MANIFEST, encoding="utf-8")
    settings_store = SettingsStore(tmp_path / "settings.yaml", defaults=RuntimeSettings())
    calls = []

    async def apply_settings(previous: RuntimeSettings, new: RuntimeSettings) -> bool:
        calls.append((previous, new))
        return False

    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(manifest_path, permitted_hosts={"example.com"}),
        settings_store=settings_store,
        admin_apply_settings=apply_settings,
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _login_headers(client)
        saved = await client.put(
            "/admin/api/settings", headers=headers, json={"confirmation_attempts": 3}
        )

    assert saved.status_code == 200
    assert saved.json() == {"status": "saved", "restart_required": False}
    assert len(calls) == 1
    assert calls[0][0].confirmation_attempts == 2
    assert calls[0][1].confirmation_attempts == 3


async def test_admin_rejects_invalid_settings_without_writing(tmp_path: Path) -> None:
    manifest_path = tmp_path / "sites.yaml"
    manifest_path.write_text(MANIFEST, encoding="utf-8")
    settings_store = SettingsStore(tmp_path / "settings.yaml", defaults=RuntimeSettings())
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(manifest_path, permitted_hosts={"example.com"}),
        settings_store=settings_store,
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _login_headers(client)
        response = await client.put(
            "/admin/api/settings",
            headers=headers,
            json={"confirmation_attempts": 99},
        )

    assert response.status_code == 422
    assert not (tmp_path / "settings.yaml").exists()


async def test_admin_rejects_oversized_payload_before_json_parsing(tmp_path: Path) -> None:
    path = tmp_path / "sites.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    app = create_app(
        engine=StubEngine(),
        webhook_token="expected-secret",
        manifest_store=ManifestStore(path, permitted_hosts={"example.com"}),
        user_store=_user_store(tmp_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _login_headers(client)
        headers["Content-Type"] = "application/json"
        response = await client.put(
            "/admin/api/manifest",
            headers=headers,
            content=b"{" + b" " * 1_100_000,
        )

    assert response.status_code == 413
