from collections.abc import MutableMapping
from pathlib import Path
from typing import cast

import pytest

from triage_agent.manifests import load_site_manifest, parse_site_manifest

VALID_MANIFEST = """
version: 1
sites:
  - id: example-site
    allowed_hosts:
      - example.com
    pages:
      - id: home
        url: https://example.com/
        viewports:
          - id: desktop
            width: 1440
            height: 900
            device_scale_factor: 1.0
          - id: mobile
            width: 390
            height: 844
            device_scale_factor: 3.0
        required_text:
          - Welcome
        required_selectors:
          - main
        forbidden_text:
          - Fatal error
        critical_resource_patterns:
          - /assets/application.js
        ignored_resource_patterns:
          - /analytics
        screenshot_masks:
          - .dynamic-banner
        application_shortcodes:
          - contact-form-7
        plugin_assertions:
          - id: contact-form
            kind: contact-form-7
            required_selectors:
              - form.wpcf7-form
              - input[name="your-email"]
              - input[type="submit"]
        wordpress_health:
          - id: site-health
            endpoint: https://example.com/wp-json/techx-monitor/v1/health
            token_secret_ref: techx-monitor-token
        interactions:
          - action: click
            selector: button[aria-expanded="false"]
"""


def test_parses_valid_declarative_site_manifest() -> None:
    registry = parse_site_manifest(VALID_MANIFEST)

    page = registry.page("home")
    assert page.url == "https://example.com/"
    assert [viewport.id for viewport in page.viewports] == ["desktop", "mobile"]
    assert page.required_selectors == ("main",)
    assert page.application_shortcodes == ("contact-form-7",)
    assert page.plugin_assertions[0].id == "contact-form"
    assert page.plugin_assertions[0].kind == "contact-form-7"
    assert page.plugin_assertions[0].required_selectors == (
        "form.wpcf7-form",
        'input[name="your-email"]',
        'input[type="submit"]',
    )
    assert page.wordpress_health[0].id == "site-health"
    assert page.wordpress_health[0].endpoint == "https://example.com/wp-json/techx-monitor/v1/health"
    assert page.wordpress_health[0].token_secret_ref == "techx-monitor-token"
    assert page.interactions[0].enabled is False
    assert registry.allowed_hosts("home") == frozenset({"example.com"})


def test_rejects_duplicate_site_ids() -> None:
    duplicate_sites = """
version: 1
sites:
  - id: repeated
    allowed_hosts: [one.example.com]
    pages:
      - id: page-one
        url: https://one.example.com/
        viewports:
          - {id: desktop, width: 1440, height: 900, device_scale_factor: 1}
  - id: repeated
    allowed_hosts: [two.example.com]
    pages:
      - id: page-two
        url: https://two.example.com/
        viewports:
          - {id: desktop, width: 1440, height: 900, device_scale_factor: 1}
"""

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(duplicate_sites)


def test_rejects_duplicate_page_ids_across_sites() -> None:
    duplicate_pages = """
version: 1
sites:
  - id: first
    allowed_hosts: [one.example.com]
    pages:
      - id: home
        url: https://one.example.com/
        viewports:
          - {id: desktop, width: 1440, height: 900, device_scale_factor: 1}
  - id: second
    allowed_hosts: [two.example.com]
    pages:
      - id: home
        url: https://two.example.com/
        viewports:
          - {id: desktop, width: 1440, height: 900, device_scale_factor: 1}
"""

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(duplicate_pages)


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://example.com/",
        "https://other.example/",
        "https://user:password@example.com/",
        "https://example.com:8443/",
    ],
)
def test_rejects_page_url_outside_exact_https_allowlist(unsafe_url: str) -> None:
    manifest = VALID_MANIFEST.replace("https://example.com/", unsafe_url)

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(manifest)


def test_rejects_duplicate_viewport_ids_within_page() -> None:
    duplicate_viewports = VALID_MANIFEST.replace("id: mobile", "id: desktop")

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(duplicate_viewports)


def test_rejects_duplicate_plugin_assertion_ids_within_page() -> None:
    duplicate_assertion = VALID_MANIFEST.replace(
        "        interactions:",
        "          - id: contact-form\n"
        "            kind: woocommerce\n"
        "            required_selectors:\n"
        "              - .woocommerce-product\n"
        "        interactions:",
    )

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(duplicate_assertion)


def test_rejects_unsupported_plugin_assertion_kind() -> None:
    unsupported_kind = VALID_MANIFEST.replace("kind: contact-form-7", "kind: unknown-plugin")

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(unsupported_kind)


def test_rejects_unknown_fields_including_unapproved_visual_baseline() -> None:
    with_baseline = VALID_MANIFEST.replace(
        "        url: https://example.com/",
        "        url: https://example.com/\n        visual_baseline: unapproved.png",
    )

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(with_baseline)


def test_rejects_destructive_interaction_verbs() -> None:
    destructive = VALID_MANIFEST.replace("action: click", "action: submit")

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(destructive)


def test_parses_optional_ready_selector() -> None:
    manifest = VALID_MANIFEST.replace(
        "        required_text:\n",
        "        ready_selector: main\n        required_text:\n",
    )

    registry = parse_site_manifest(manifest)

    assert registry.page("home").ready_selector == "main"


def test_ready_selector_defaults_to_none() -> None:
    registry = parse_site_manifest(VALID_MANIFEST)

    assert registry.page("home").ready_selector is None


def test_parses_fill_interaction_with_value() -> None:
    manifest = VALID_MANIFEST.replace(
        '          - action: click\n            selector: button[aria-expanded="false"]\n',
        "          - action: fill\n"
        '            selector: input[name="s"]\n'
        "            value: query\n",
    )

    registry = parse_site_manifest(manifest)

    interaction = registry.page("home").interactions[0]
    assert interaction.action == "fill"
    assert interaction.value == "query"


def test_fill_interaction_allows_deliberately_incomplete_empty_value() -> None:
    manifest = VALID_MANIFEST.replace(
        '          - action: click\n            selector: button[aria-expanded="false"]\n',
        '          - action: fill\n            selector: input[required]\n            value: ""\n',
    )

    registry = parse_site_manifest(manifest)

    assert registry.page("home").interactions[0].value == ""


def test_rejects_fill_interaction_without_value() -> None:
    missing_value = VALID_MANIFEST.replace("action: click", "action: fill")

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(missing_value)


def test_rejects_click_interaction_with_value() -> None:
    stray_value = VALID_MANIFEST.replace(
        '          - action: click\n            selector: button[aria-expanded="false"]\n',
        "          - action: click\n"
        '            selector: button[aria-expanded="false"]\n'
        "            value: query\n",
    )

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(stray_value)


def test_rejects_invalid_resource_patterns() -> None:
    invalid_pattern = VALID_MANIFEST.replace("/assets/application.js", "(")

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(invalid_pattern)


def test_rejects_pathological_regular_expression_resource_patterns() -> None:
    pathological = VALID_MANIFEST.replace("/assets/application.js", "(a+)+$")

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(pathological)


def test_loads_checked_in_example_manifest() -> None:
    registry = load_site_manifest(Path(__file__).parents[1] / "config" / "sites.example.yaml")

    assert registry.page("example-home").url == "https://example.com/"


def test_file_loader_rejects_manifest_larger_than_one_mebibyte(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(ValueError, match="too large"):
        load_site_manifest(oversized)


def test_direct_parser_rejects_manifest_larger_than_one_mebibyte() -> None:
    oversized = VALID_MANIFEST + "\n# " + ("x" * (1024 * 1024))

    with pytest.raises(ValueError, match="too large"):
        parse_site_manifest(oversized)


@pytest.mark.parametrize(
    "ambiguous_manifest",
    [
        VALID_MANIFEST.replace(
            "        url: https://example.com/",
            "        url: http://unsafe.example/\n        url: https://example.com/",
        ),
        VALID_MANIFEST.replace(
            "        required_text:\n          - Welcome\n"
            "        required_selectors:\n          - main",
            "        required_text: &shared\n          - Welcome\n"
            "        required_selectors: *shared",
        ),
    ],
)
def test_rejects_duplicate_yaml_keys_and_aliases(ambiguous_manifest: str) -> None:
    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(ambiguous_manifest)


def test_manifest_page_registry_cannot_be_mutated_after_validation() -> None:
    registry = parse_site_manifest(VALID_MANIFEST)
    pages = cast(MutableMapping[str, object], registry._pages)

    with pytest.raises(TypeError):
        pages["injected"] = registry.page("home")


def test_rejects_coerced_scalar_types() -> None:
    quoted_width = VALID_MANIFEST.replace("            width: 1440", '            width: "1440"')

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(quoted_width)


def test_rejects_boolean_manifest_version() -> None:
    boolean_version = VALID_MANIFEST.replace("version: 1", "version: true")

    with pytest.raises(ValueError, match="Invalid site manifest"):
        parse_site_manifest(boolean_version)
