import asyncio
import gzip
import hashlib
import socket
import threading
from pathlib import Path

import httpx
import pytest

from triage_agent.browser_checks import InteractionResult, PluginAssertionResult, TextResult
from triage_agent.manifests import (
    InteractionManifest,
    PageManifest,
    PluginAssertionKind,
    PluginAssertionManifest,
    ViewportManifest,
)
from triage_agent.security import UnsafeTargetError


class FakeRequest:
    def __init__(self, url: str, method: str = "GET") -> None:
        self.url = url
        self.method = method


class FakeRoute:
    def __init__(self, url: str, method: str = "GET") -> None:
        self.request = FakeRequest(url, method)
        self.aborted_with: str | None = None

    async def abort(self, error_code: str = "failed") -> None:
        self.aborted_with = error_code

    async def fulfill(
        self,
        *,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        raise AssertionError("Unsafe requests must not be fulfilled")


async def test_browser_url_policy_rejects_non_https_before_dns() -> None:
    resolver_called = False

    async def resolver(host: str) -> set[str]:
        nonlocal resolver_called
        resolver_called = True
        return {"93.184.216.34"}

    from triage_agent.browser_runner import authorize_browser_url

    with pytest.raises(UnsafeTargetError):
        await authorize_browser_url(
            "http://example.com/",
            allowed_hosts={"example.com"},
            resolver=resolver,
        )

    assert resolver_called is False


async def test_browser_resource_fetch_is_pinned_to_validated_address() -> None:
    async def resolver(host: str) -> set[str]:
        assert host == "example.com"
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main>Welcome</main>",
        )

    from triage_agent.browser_runner import fetch_browser_resource

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await fetch_browser_resource(
            "https://example.com/",
            allowed_hosts={"example.com"},
            resolver=resolver,
            client=client,
        )

    assert response.status_code == 200
    assert response.body == b"<main>Welcome</main>"


async def test_browser_resource_removes_encoding_after_httpx_decodes_body() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-encoding": "gzip",
                "content-type": "text/html; charset=utf-8",
            },
            content=gzip.compress(b"<main>Welcome</main>"),
        )

    from triage_agent.browser_runner import fetch_browser_resource

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await fetch_browser_resource(
            "https://example.com/",
            allowed_hosts={"example.com"},
            resolver=resolver,
            client=client,
        )

    assert response.body == b"<main>Welcome</main>"
    assert "content-encoding" not in dict(response.headers)


async def test_browser_resource_does_not_share_httpx_cookies_between_hosts() -> None:
    observed: list[tuple[str, str | None]] = []

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.headers["host"], request.headers.get("cookie")))
        if request.headers["host"] == "a.example.com":
            return httpx.Response(
                200,
                headers={"set-cookie": "SECRET=from-a; Secure"},
            )
        return httpx.Response(200)

    from triage_agent.browser_runner import fetch_browser_resource

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_browser_resource(
            "https://a.example.com/",
            allowed_hosts={"a.example.com", "b.example.com"},
            resolver=resolver,
            client=client,
        )
        await fetch_browser_resource(
            "https://b.example.com/",
            allowed_hosts={"a.example.com", "b.example.com"},
            resolver=resolver,
            client=client,
        )

    assert observed == [
        ("a.example.com", None),
        ("b.example.com", None),
    ]


async def test_browser_route_aborts_unsafe_subresource_before_dns() -> None:
    resolver_called = False

    async def resolver(host: str) -> set[str]:
        nonlocal resolver_called
        resolver_called = True
        return {"127.0.0.1"}

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Unsafe requests must not reach the HTTP client")

    from triage_agent.browser_runner import handle_browser_route

    route = FakeRoute("http://127.0.0.1/internal")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        await handle_browser_route(
            route,
            allowed_hosts={"example.com"},
            resolver=resolver,
            client=client,
        )

    assert resolver_called is False
    assert route.aborted_with == "blockedbyclient"


async def test_browser_route_rejects_unsafe_redirect_location_before_dns() -> None:
    resolved_hosts: list[str] = []

    async def resolver(host: str) -> set[str]:
        resolved_hosts.append(host)
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://127.0.0.1/internal"},
        )

    from triage_agent.browser_runner import handle_browser_route

    route = FakeRoute("https://example.com/")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await handle_browser_route(
            route,
            allowed_hosts={"example.com"},
            resolver=resolver,
            client=client,
        )

    assert result is None
    assert resolved_hosts == ["example.com"]
    assert route.aborted_with == "blockedbyclient"


async def test_browser_route_aborts_non_read_only_method_before_dns() -> None:
    resolver_called = False

    async def resolver(host: str) -> set[str]:
        nonlocal resolver_called
        resolver_called = True
        return {"93.184.216.34"}

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("A non-read-only request must not reach transport")

    from triage_agent.browser_runner import handle_browser_route

    route = FakeRoute("https://example.com/actions", method="POST")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        result = await handle_browser_route(
            route,
            allowed_hosts={"example.com"},
            resolver=resolver,
            client=client,
        )

    assert result is None
    assert resolver_called is False
    assert route.aborted_with == "blockedbyclient"


def test_browser_context_options_are_deterministic_and_restrictive() -> None:
    from triage_agent.browser_runner import build_browser_context_options

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )

    assert build_browser_context_options(viewport) == {
        "viewport": {"width": 1440, "height": 900},
        "device_scale_factor": 1.0,
        "user_agent": "Web-Assurance-Agent/0.2.0",
        "locale": "en-US",
        "timezone_id": "UTC",
        "color_scheme": "light",
        "reduced_motion": "reduce",
        "service_workers": "block",
        "accept_downloads": False,
        "ignore_https_errors": False,
    }


async def test_playwright_runner_captures_intercepted_healthy_page() -> None:
    async def resolver(host: str) -> set[str]:
        assert host == "example.com"
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(b"<!doctype html><title>Fixture</title><main><h1>Welcome</h1></main>"),
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(
            ViewportManifest(
                id="desktop",
                width=1440,
                height=900,
                device_scale_factor=1.0,
            ),
        ),
        required_text=("Welcome",),
        required_selectors=("main",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
        ).run(
            page=page,
            viewport=page.viewports[0],
            allowed_hosts={"example.com"},
        )

    assert evidence.document_status == 200
    assert evidence.title == "Fixture"
    assert evidence.required_text_results[0].found is True
    assert evidence.required_selector_results[0].visible is True
    assert evidence.required_selector_results[0].width > 0
    assert evidence.required_selector_results[0].height > 0
    assert evidence.timed_out is False
    assert evidence.page_width > 0
    assert evidence.page_height > 0
    assert evidence.browser_version != ""


async def test_playwright_runner_captures_evidence_despite_strict_style_csp() -> None:
    async def resolver(host: str) -> set[str]:
        assert host == "example.com"
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html; charset=utf-8",
                "content-security-policy": "style-src 'self'",
            },
            content=(b"<!doctype html><title>Fixture</title><main><h1>Welcome</h1></main>"),
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(
            ViewportManifest(
                id="desktop",
                width=1440,
                height=900,
                device_scale_factor=1.0,
            ),
        ),
        required_text=("Welcome",),
        required_selectors=("main",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
        ).run(
            page=page,
            viewport=page.viewports[0],
            allowed_hosts={"example.com"},
        )

    # A style-src CSP blocks our best-effort scroll-behavior/animation CSS
    # injection; that must not abort evidence collection for the rest of the
    # page (screenshot suppression already happens CSP-safely via the
    # `animations`/`caret` screenshot options).
    assert evidence.timed_out is False
    assert evidence.document_status == 200
    assert evidence.title == "Fixture"
    assert evidence.required_text_results == (TextResult(value="Welcome", found=True),)


async def test_playwright_runner_waits_for_ready_selector_before_capturing() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b'<main id="app-ready">Loaded</main>',
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        ready_selector="#app-ready",
        required_text=("Loaded",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.timed_out is False
    assert evidence.required_text_results[0].found is True


async def test_playwright_runner_times_out_when_ready_selector_never_appears() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main>No matching element</main>",
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        ready_selector="#never-appears",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(
            client=client, resolver=resolver, total_timeout_ms=1_000
        ).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.timed_out is True


async def test_playwright_runner_captures_collapsed_required_geometry() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                b"<style>main{width:0;height:0;overflow:hidden}</style><main>Collapsed</main>"
            ),
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        required_selectors=("main",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.required_selector_results[0].width == 0
    assert evidence.required_selector_results[0].height == 0


async def test_playwright_runner_captures_satisfied_plugin_assertion() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                b'<form class="wpcf7-form"><input name="your-email"><input type="submit"></form>'
            ),
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        plugin_assertions=(
            PluginAssertionManifest(
                id="contact-form",
                kind="contact-form-7",
                required_selectors=("form.wpcf7-form", 'input[name="your-email"]'),
            ),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.plugin_assertion_results == (
        PluginAssertionResult(assertion_id="contact-form", kind="contact-form-7", satisfied=True),
    )


@pytest.mark.parametrize(
    ("body", "description"),
    [
        (b"<p>No form here</p>", "missing selector"),
        (
            b'<form class="wpcf7-form" style="display:none"><input name="your-email"></form>',
            "hidden selector",
        ),
        (
            b"<style>.wpcf7-form{width:0;height:0;overflow:hidden}</style>"
            b'<form class="wpcf7-form"><input name="your-email"></form>',
            "zero-geometry selector",
        ),
    ],
    ids=["missing", "hidden", "zero-geometry"],
)
async def test_playwright_runner_captures_failed_plugin_assertion(
    body: bytes,
    description: str,
) -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=body,
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        plugin_assertions=(
            PluginAssertionManifest(
                id="contact-form",
                kind="contact-form-7",
                required_selectors=("form.wpcf7-form", 'input[name="your-email"]'),
            ),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.plugin_assertion_results == (
        PluginAssertionResult(assertion_id="contact-form", kind="contact-form-7", satisfied=False),
    ), description


@pytest.mark.parametrize(
    ("kind", "selector"),
    [
        ("elementor", ".elementor-section"),
        ("contact-form-7", ".wpcf7-form"),
        ("woocommerce", ".woocommerce-product"),
        ("gallery-slider", ".slick-slide"),
        ("search", ".search-results"),
        ("multilingual", ".language-switcher"),
    ],
)
async def test_playwright_runner_captures_plugin_assertions_for_every_supported_kind(
    kind: PluginAssertionKind,
    selector: str,
) -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=f'<div class="{selector.removeprefix(".")}">rendered</div>'.encode(),
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        plugin_assertions=(
            PluginAssertionManifest(id="assertion", kind=kind, required_selectors=(selector,)),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.plugin_assertion_results == (
        PluginAssertionResult(assertion_id="assertion", kind=kind, satisfied=True),
    )


async def test_playwright_runner_executes_enabled_click_interaction() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b'<button class="nav-toggle" aria-expanded="false">Menu</button>',
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        interactions=(
            InteractionManifest(action="click", selector="button.nav-toggle", enabled=True),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.interaction_results == (
        InteractionResult(action="click", selector="button.nav-toggle", succeeded=True),
    )


async def test_playwright_runner_executes_enabled_fill_interaction() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b'<input name="s" type="text">',
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        interactions=(
            InteractionManifest(
                action="fill", selector="input[name='s']", value="query", enabled=True
            ),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.interaction_results == (
        InteractionResult(action="fill", selector="input[name='s']", succeeded=True),
    )


async def test_playwright_runner_records_failed_interaction_for_missing_selector() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main>No matching element</main>",
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        interactions=(
            InteractionManifest(action="click", selector="button.nav-toggle", enabled=True),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.interaction_results == (
        InteractionResult(action="click", selector="button.nav-toggle", succeeded=False),
    )


async def test_playwright_runner_skips_disabled_interaction() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b'<button class="nav-toggle" aria-expanded="false">Menu</button>',
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(id="desktop", width=1440, height=900, device_scale_factor=1.0)
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        interactions=(
            InteractionManifest(action="click", selector="button.nav-toggle", enabled=False),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.interaction_results == ()


async def test_playwright_runner_captures_console_and_page_errors() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                b"<!doctype html><title>Fixture</title><main>Welcome</main>"
                b"<script>console.error('CONSOLE_SENTINEL');"
                b"setTimeout(() => { throw new Error('EXCEPTION_SENTINEL'); }, 0);</script>"
            ),
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.console_errors == ("console_error",)
    assert evidence.page_exceptions == ("page_exception",)


async def test_playwright_runner_captures_critical_resource_failure() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/assets/application.js":
            return httpx.Response(
                503,
                headers={"content-type": "application/javascript"},
                content=b"",
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main>Welcome</main><script src='/assets/application.js'></script>",
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        critical_resource_patterns=("/assets/application.js",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert len(evidence.resource_failures) == 1
    assert evidence.resource_failures[0].status_code == 503
    assert evidence.resource_failures[0].critical is True


async def test_playwright_runner_records_policy_blocked_critical_resource() -> None:
    resolved_hosts: list[str] = []

    async def resolver(host: str) -> set[str]:
        resolved_hosts.append(host)
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                b"<main>Welcome</main><script src='https://127.0.0.1/assets/unsafe.js'></script>"
            ),
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        critical_resource_patterns=("/assets/unsafe.js",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert resolved_hosts == ["example.com"]
    assert len(evidence.resource_failures) == 1
    assert evidence.resource_failures[0].status_code is None
    assert evidence.resource_failures[0].critical is True


async def test_playwright_runner_returns_typed_evidence_on_total_timeout() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def delayed_request(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, content=b"<main>Too late</main>")

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        required_text=("Welcome",),
        required_selectors=("main",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(delayed_request)) as client:
        evidence = await PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
            total_timeout_ms=1_000,
        ).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.timed_out is True
    assert evidence.document_status is None


async def test_playwright_runner_revalidates_unsafe_redirect_before_dns() -> None:
    resolved_hosts: list[str] = []

    async def resolver(host: str) -> set[str]:
        resolved_hosts.append(host)
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        return httpx.Response(
            302,
            headers={"location": "https://127.0.0.1/internal"},
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert resolved_hosts == ["example.com"]
    assert evidence.document_status is None
    assert evidence.timed_out is False


async def test_playwright_runner_normalizes_resolver_failure() -> None:
    async def resolver(host: str) -> set[str]:
        raise OSError("DNS_SENTINEL")

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient() as client:
        evidence = await PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
        ).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.document_status is None
    assert evidence.timed_out is False
    assert evidence.console_errors == ()
    assert evidence.page_exceptions == ()


async def test_playwright_runner_follows_allowed_redirect_through_pinned_fetch() -> None:
    async def resolver(host: str) -> set[str]:
        return {
            "example.com": {"93.184.216.34"},
            "monitor.example.com": {"93.184.216.35"},
        }[host]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "93.184.216.34":
            return httpx.Response(
                302,
                headers={"location": "https://monitor.example.com/status"},
            )
        assert request.url.host == "93.184.216.35"
        assert request.headers["host"] == "monitor.example.com"
        assert request.extensions["sni_hostname"] == "monitor.example.com"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<title>Status</title><main>Healthy</main>",
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="status",
        url="https://example.com/",
        viewports=(viewport,),
        required_text=("Healthy",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com", "monitor.example.com"},
        )

    assert evidence.document_status == 200
    assert evidence.final_url == "https://monitor.example.com/status"
    assert evidence.required_text_results[0].found is True


async def test_redirected_document_resolves_relative_resource_on_final_host() -> None:
    requests: list[tuple[str, str]] = []

    async def resolver(host: str) -> set[str]:
        return {
            "example.com": {"93.184.216.34"},
            "monitor.example.com": {"93.184.216.35"},
        }[host]

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.host, request.url.path))
        if request.url.host == "93.184.216.34" and request.url.path == "/":
            return httpx.Response(
                302,
                headers={"location": "https://monitor.example.com/status"},
            )
        if request.url.host == "93.184.216.35" and request.url.path == "/status":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<main>Healthy</main><script src='/assets/final.js'></script>",
            )
        if request.url.host == "93.184.216.35" and request.url.path == "/assets/final.js":
            return httpx.Response(
                200,
                headers={"content-type": "application/javascript"},
                content=b"window.fixtureLoaded = true;",
            )
        return httpx.Response(404)

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="status",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com", "monitor.example.com"},
        )

    assert ("93.184.216.35", "/assets/final.js") in requests
    assert ("93.184.216.34", "/assets/final.js") not in requests


async def test_playwright_runner_captures_opt_in_screenshot_metadata(
    tmp_path: Path,
) -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main>Welcome</main><aside class='dynamic'>Changing</aside>",
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="mobile",
        width=640,
        height=480,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        screenshot_masks=(".dynamic",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
            artifact_directory=tmp_path,
        ).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.screenshot is not None
    screenshot_path = Path(evidence.screenshot.path)
    assert screenshot_path.parent == tmp_path
    assert screenshot_path.name.startswith("home-mobile-")
    assert screenshot_path.suffix == ".png"
    assert screenshot_path.is_file()
    screenshot_bytes = screenshot_path.read_bytes()
    assert evidence.screenshot.sha256 == hashlib.sha256(screenshot_bytes).hexdigest()
    assert evidence.screenshot.width == 640
    assert evidence.screenshot.height == 480


async def test_playwright_runner_preserves_cors_across_cross_host_redirect() -> None:
    async def resolver(host: str) -> set[str]:
        return {
            "example.com": {"93.184.216.34"},
            "api.example.com": {"93.184.216.35"},
        }[host]

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        if host == "example.com" and request.url.path == "/":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=(
                    b"<main>Ready</main><script>"
                    b"fetch('/data').then(r=>r.text()).then(t=>{"
                    b"document.body.textContent=t})"
                    b"</script>"
                ),
            )
        if host == "example.com" and request.url.path == "/data":
            return httpx.Response(
                302,
                headers={"location": "https://api.example.com/private"},
            )
        assert host == "api.example.com"
        assert request.url.path == "/private"
        return httpx.Response(200, content=b"CROSS_ORIGIN_SECRET")

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
        forbidden_text=("CROSS_ORIGIN_SECRET",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com", "api.example.com"},
        )

    assert evidence.forbidden_text_matches == ()


async def test_playwright_runner_preserves_duplicate_set_cookie_headers() -> None:
    observed_cookie: str | None = None

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_cookie
        if request.url.path == "/":
            return httpx.Response(
                200,
                headers=[
                    ("content-type", "text/html; charset=utf-8"),
                    ("set-cookie", "first=one; Path=/; Secure; SameSite=Lax"),
                    ("set-cookie", "second=two; Path=/; Secure; SameSite=Lax"),
                ],
                content=b"<script>fetch('/cookie-check')</script>",
            )
        observed_cookie = request.headers.get("cookie")
        return httpx.Response(204)

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.document_status == 200
    assert observed_cookie is not None
    assert "first=one" in observed_cookie
    assert "second=two" in observed_cookie


async def test_playwright_runner_blocks_webrtc_stun_udp_to_private_address() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(2.0)
    stun_port = listener.getsockname()[1]
    received_packets: list[bytes] = []

    def receive_packet() -> None:
        try:
            packet, _ = listener.recvfrom(2048)
            received_packets.append(packet)
        except TimeoutError:
            pass

    receiver = threading.Thread(target=receive_packet)
    receiver.start()

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                b"<main>Ready</main><script>"
                b"const pc=new RTCPeerConnection({iceServers:[{urls:'stun:"
                + f"127.0.0.1:{stun_port}".encode()
                + b"'}]});pc.createDataChannel('probe');"
                b"pc.createOffer().then(o=>pc.setLocalDescription(o));"
                b"</script>"
            ),
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=1440,
        height=900,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
                page=page,
                viewport=viewport,
                allowed_hosts={"example.com"},
            )
        receiver.join(timeout=3.0)
    finally:
        listener.close()
        receiver.join(timeout=3.0)

    assert evidence.document_status == 200
    assert received_packets == []


async def test_playwright_runner_publishes_concurrent_screenshots_independently(
    tmp_path: Path,
) -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main>Concurrent fixture</main>",
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=640,
        height=480,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
            artifact_directory=tmp_path,
        )
        first, second = await asyncio.gather(
            runner.run(
                page=page,
                viewport=viewport,
                allowed_hosts={"example.com"},
            ),
            runner.run(
                page=page,
                viewport=viewport,
                allowed_hosts={"example.com"},
            ),
        )

    assert first.screenshot is not None
    assert second.screenshot is not None
    assert first.screenshot.path != second.screenshot.path
    for screenshot in (first.screenshot, second.screenshot):
        content = Path(screenshot.path).read_bytes()
        assert screenshot.sha256 == hashlib.sha256(content).hexdigest()


async def test_browser_redirect_chain_scopes_cookies_to_original_hosts() -> None:
    observed: list[tuple[str, str, str | None]] = []

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        observed.append((host, request.url.path, request.headers.get("cookie")))
        if request.url.path == "/same-start":
            return httpx.Response(
                302,
                headers={
                    "location": "https://a.example.com/same-final",
                    "set-cookie": "session=same-host; Path=/; Secure",
                },
            )
        if request.url.path == "/cross-start":
            return httpx.Response(
                302,
                headers={
                    "location": "https://b.example.com/cross-final",
                    "set-cookie": "secret=host-only; Path=/; Secure",
                },
            )
        return httpx.Response(200)

    from triage_agent.browser_runner import fetch_browser_redirect_chain

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_browser_redirect_chain(
            "https://a.example.com/same-start",
            allowed_hosts={"a.example.com", "b.example.com"},
            resolver=resolver,
            client=client,
            allow_cross_origin=True,
        )
        await fetch_browser_redirect_chain(
            "https://a.example.com/cross-start",
            allowed_hosts={"a.example.com", "b.example.com"},
            resolver=resolver,
            client=client,
            allow_cross_origin=True,
        )

    assert ("a.example.com", "/same-final", "session=same-host") in observed
    assert ("b.example.com", "/cross-final", None) in observed


async def test_playwright_runner_seeds_scoped_redirect_cookies_into_context() -> None:
    observed_check_cookie: str | None = None

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_check_cookie
        if request.url.path == "/":
            return httpx.Response(
                302,
                headers={
                    "location": "https://example.com/final",
                    "set-cookie": "session=needed; Path=/; Secure; HttpOnly",
                },
            )
        if request.url.path == "/final":
            assert request.headers.get("cookie") == "session=needed"
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<script>fetch('/cookie-check')</script>",
            )
        observed_check_cookie = request.headers.get("cookie")
        return httpx.Response(204)

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=640,
        height=480,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.document_status == 200
    assert observed_check_cookie == "session=needed"


async def test_playwright_runner_normalizes_screenshot_publication_failure(
    tmp_path: Path,
) -> None:
    invalid_artifact_directory = tmp_path / "not-a-directory"
    invalid_artifact_directory.write_text("occupied", encoding="utf-8")

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main>Ready</main>",
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=640,
        height=480,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
            artifact_directory=invalid_artifact_directory,
        ).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.document_status is None
    assert evidence.screenshot is None
    assert evidence.timed_out is False


async def test_playwright_runner_cancels_active_route_tasks_on_total_timeout() -> None:
    route_started = asyncio.Event()
    route_cancelled = asyncio.Event()

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<script src='/slow.js'></script>",
            )
        route_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            route_cancelled.set()
            raise
        raise AssertionError("The delayed route must be cancelled")

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=640,
        height=480,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
            total_timeout_ms=1_500,
        ).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert route_started.is_set()
    assert route_cancelled.is_set()
    assert evidence.timed_out is True


async def test_browser_redirect_chain_merges_existing_and_new_cookies() -> None:
    redirected_cookies: list[str] = []

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "location": "https://example.com/final",
                    "set-cookie": "session=new; Path=/; Secure",
                },
            )
        redirected_cookies.extend(request.headers.get_list("cookie"))
        return httpx.Response(200)

    from triage_agent.browser_runner import fetch_browser_redirect_chain

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_browser_redirect_chain(
            "https://example.com/start",
            allowed_hosts={"example.com"},
            resolver=resolver,
            client=client,
            request_headers={"Cookie": "session=existing; unrelated=keep"},
            allow_cross_origin=False,
        )

    assert redirected_cookies == ["session=new; unrelated=keep"]


def test_playwright_cookie_conversion_normalizes_samesite_case() -> None:
    from triage_agent.browser_runner import BrowserCookie, _playwright_cookies

    cookie = BrowserCookie(
        name="session",
        value="value",
        domain="example.com",
        domain_initial_dot=False,
        path="/",
        expires=None,
        secure=True,
        http_only=True,
        same_site="none",
    )

    assert _playwright_cookies((cookie,))[0]["sameSite"] == "None"


async def test_playwright_runner_bounds_request_burst_and_concurrency() -> None:
    request_count = 0
    active_requests = 0
    peak_concurrency = 0

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, peak_concurrency, request_count
        request_count += 1
        if request.url.path == "/":
            scripts = b"".join(
                f"<script src='/asset-{index}.js'></script>".encode() for index in range(100)
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=scripts,
            )
        active_requests += 1
        peak_concurrency = max(peak_concurrency, active_requests)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(
                200,
                headers={"content-type": "application/javascript"},
                content=b"void 0;",
            )
        finally:
            active_requests -= 1

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=640,
        height=480,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert request_count <= 64
    assert peak_concurrency <= 8


async def test_browser_run_budget_bounds_aggregate_decoded_bytes() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abc")

    from triage_agent.browser_runner import (
        BrowserFetchError,
        BrowserRunBudget,
        fetch_browser_resource,
    )

    budget = BrowserRunBudget(max_requests=2, max_total_bytes=5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_browser_resource(
            "https://example.com/first",
            allowed_hosts={"example.com"},
            resolver=resolver,
            client=client,
            budget=budget,
        )
        with pytest.raises(BrowserFetchError, match="byte budget"):
            await fetch_browser_resource(
                "https://example.com/second",
                allowed_hosts={"example.com"},
                resolver=resolver,
                client=client,
                budget=budget,
            )

    assert budget.total_bytes == 3


async def test_subresource_redirect_cookies_update_browser_context() -> None:
    final_cookie: str | None = None
    check_cookie: str | None = None

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal check_cookie, final_cookie
        if request.url.path == "/":
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "set-cookie": "session=old; Path=/; Secure",
                },
                content=(
                    b"<main>Ready</main><script>"
                    b"fetch('/start').then(() => fetch('/check'));"
                    b"</script>"
                ),
            )
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "location": "https://example.com/final",
                    "set-cookie": "session=new; Path=/; Secure",
                },
            )
        if request.url.path == "/final":
            final_cookie = request.headers.get("cookie")
            return httpx.Response(200)
        if request.url.path == "/check":
            check_cookie = request.headers.get("cookie")
            return httpx.Response(200)
        raise AssertionError("unexpected request")

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=640,
        height=480,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(client=client, resolver=resolver).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.document_status == 200
    assert final_cookie == "session=new"
    assert check_cookie == "session=new"


async def test_redirect_cookie_attributes_are_case_insensitive() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"set-cookie": ("session=value; Path=/; Secure; httponly; samesite=strict")},
        )

    from triage_agent.browser_runner import fetch_browser_redirect_chain

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await fetch_browser_redirect_chain(
            "https://example.com/",
            allowed_hosts={"example.com"},
            resolver=resolver,
            client=client,
            allow_cross_origin=False,
        )

    assert len(response.cookies) == 1
    assert response.cookies[0].http_only is True
    assert response.cookies[0].same_site == "strict"


async def test_playwright_screenshot_is_viewport_bounded_for_tall_page(
    tmp_path: Path,
) -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main style='height:100000px'>Tall</main>",
        )

    from triage_agent.browser_runner import PlaywrightBrowserRunner

    viewport = ViewportManifest(
        id="desktop",
        width=320,
        height=240,
        device_scale_factor=1.0,
    )
    page = PageManifest(
        id="home",
        url="https://example.com/",
        viewports=(viewport,),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await PlaywrightBrowserRunner(
            client=client,
            resolver=resolver,
            artifact_directory=tmp_path,
        ).run(
            page=page,
            viewport=viewport,
            allowed_hosts={"example.com"},
        )

    assert evidence.screenshot is not None
    assert evidence.screenshot.width == viewport.width
    assert evidence.screenshot.height == viewport.height


async def test_pinned_resources_use_distinct_single_use_transports() -> None:
    factory_calls = 0

    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    def transport_factory() -> httpx.AsyncBaseTransport:
        nonlocal factory_calls
        factory_calls += 1
        return httpx.MockTransport(handler)

    from triage_agent.browser_runner import fetch_browser_resource

    await fetch_browser_resource(
        "https://a.example.com/",
        allowed_hosts={"a.example.com", "b.example.com"},
        resolver=resolver,
        transport_factory=transport_factory,
    )
    await fetch_browser_resource(
        "https://b.example.com/",
        allowed_hosts={"a.example.com", "b.example.com"},
        resolver=resolver,
        transport_factory=transport_factory,
    )

    assert factory_calls == 2


async def test_browser_fetch_rejects_non_mock_shared_client() -> None:
    async def resolver(host: str) -> set[str]:
        return {"93.184.216.34"}

    from triage_agent.browser_runner import BrowserFetchError, fetch_browser_resource

    async with httpx.AsyncClient() as client:
        with pytest.raises(BrowserFetchError, match="Shared browser HTTP clients"):
            await fetch_browser_resource(
                "https://example.com/",
                allowed_hosts={"example.com"},
                resolver=resolver,
                client=client,
            )


def test_screenshot_viewport_pixel_budget_is_checked_before_capture() -> None:
    from triage_agent.browser_runner import (
        BrowserFetchError,
        _validate_screenshot_viewport,
    )

    viewport = ViewportManifest(
        id="oversized",
        width=7680,
        height=7680,
        device_scale_factor=1.0,
    )

    with pytest.raises(BrowserFetchError, match="pixel limit"):
        _validate_screenshot_viewport(viewport)
