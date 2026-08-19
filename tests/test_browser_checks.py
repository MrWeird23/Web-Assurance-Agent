from dataclasses import replace
from typing import cast, get_args

import pytest

from triage_agent.browser_checks import (
    PLUGIN_ASSERTION_KINDS,
    BrowserEvidence,
    InteractionResult,
    PluginAssertionResult,
    ResourceFailure,
    ScreenshotArtifact,
    SelectorResult,
    TextResult,
    Viewport,
    VisualAssuranceResult,
    evaluate_browser_evidence,
)
from triage_agent.manifests import PluginAssertionKind


def healthy_evidence() -> BrowserEvidence:
    return BrowserEvidence(
        page_id="home",
        requested_url="https://example.com/",
        final_url="https://example.com/",
        viewport=Viewport(width=1440, height=900, device_scale_factor=1.0),
        page_width=1440,
        page_height=2200,
        device_profile="desktop",
        document_status=200,
        title="Example Site",
        browser_version="120.0.6099.109",
        required_text_results=(TextResult(value="Welcome", found=True),),
        required_selector_results=(
            SelectorResult(
                selector="main",
                found=True,
                visible=True,
                width=1200,
                height=700,
            ),
        ),
        forbidden_text_matches=(),
        application_failure_codes=(),
        plugin_assertion_results=(),
        interaction_results=(),
        console_errors=(),
        page_exceptions=(),
        resource_failures=(),
        duration_ms=840,
        timed_out=False,
        screenshot=ScreenshotArtifact(
            path="artifacts/home-desktop.png",
            sha256="a" * 64,
            width=1440,
            height=900,
        ),
    )


def test_healthy_browser_evidence_evaluates_healthy() -> None:
    result = evaluate_browser_evidence(healthy_evidence())

    assert result.healthy is True
    assert result.failures == ()
    assert result.information == ()


def test_unavailable_visual_evaluation_fails_without_aborting_browser_evidence() -> None:
    evidence = replace(
        healthy_evidence(),
        visual_assurance=VisualAssuranceResult(
            status="unavailable",
            exceeds_threshold=False,
            changed_pixel_percentage=None,
            changed_region_count=0,
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["visual_evaluation_failed"]


def test_contradictory_visual_evidence_is_invalid() -> None:
    evidence = replace(
        healthy_evidence(),
        visual_assurance=VisualAssuranceResult(
            status="matched",
            exceeds_threshold=True,
            changed_pixel_percentage=0.0,
            changed_region_count=0,
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert [finding.code for finding in result.failures] == ["invalid_browser_evidence"]


def test_detected_application_failure_code_fails_without_exposing_page_text() -> None:
    evidence = replace(
        healthy_evidence(),
        application_failure_codes=("wordpress_critical_error",),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [(finding.code, finding.message) for finding in result.failures] == [
        (
            "application_failure",
            "Detected application failure: wordpress_critical_error",
        )
    ]


def test_unknown_application_failure_code_invalidates_evidence() -> None:
    evidence = replace(healthy_evidence(), application_failure_codes=("arbitrary",))

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["invalid_browser_evidence"]


def test_failed_plugin_assertion_fails_with_stable_code() -> None:
    evidence = replace(
        healthy_evidence(),
        plugin_assertion_results=(
            PluginAssertionResult(
                assertion_id="contact-form",
                kind="contact-form-7",
                satisfied=False,
            ),
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [(finding.code, finding.message) for finding in result.failures] == [
        ("plugin_assertion_failed", "Plugin assertion failed: contact-form")
    ]


def test_satisfied_plugin_assertion_remains_healthy() -> None:
    evidence = replace(
        healthy_evidence(),
        plugin_assertion_results=(
            PluginAssertionResult(
                assertion_id="product-component",
                kind="woocommerce",
                satisfied=True,
            ),
        ),
    )

    assert evaluate_browser_evidence(evidence).healthy is True


def test_unsupported_plugin_assertion_kind_invalidates_evidence() -> None:
    evidence = replace(
        healthy_evidence(),
        plugin_assertion_results=(
            PluginAssertionResult(
                assertion_id="contact-form",
                kind="arbitrary-plugin",
                satisfied=True,
            ),
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["invalid_browser_evidence"]


def test_plugin_assertion_kinds_match_the_manifest_schema() -> None:
    assert frozenset(get_args(PluginAssertionKind)) == PLUGIN_ASSERTION_KINDS


def test_failed_interaction_fails_with_stable_code_without_exposing_selector() -> None:
    evidence = replace(
        healthy_evidence(),
        interaction_results=(
            InteractionResult(action="click", selector="button.nav-toggle", succeeded=False),
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [(finding.code, finding.message) for finding in result.failures] == [
        ("interaction_failed", "A safe click interaction failed.")
    ]
    assert all("button.nav-toggle" not in finding.message for finding in result.failures)


def test_succeeded_interaction_remains_healthy() -> None:
    evidence = replace(
        healthy_evidence(),
        interaction_results=(
            InteractionResult(action="fill", selector="input[name='s']", succeeded=True),
        ),
    )

    assert evaluate_browser_evidence(evidence).healthy is True


def test_unknown_interaction_action_invalidates_evidence() -> None:
    evidence = replace(
        healthy_evidence(),
        interaction_results=(InteractionResult(action="submit", selector="form", succeeded=True),),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["invalid_browser_evidence"]


def test_missing_required_selector_fails() -> None:
    evidence = healthy_evidence()
    missing_main = replace(
        evidence.required_selector_results[0],
        found=False,
        visible=False,
        width=0,
        height=0,
    )

    result = evaluate_browser_evidence(replace(evidence, required_selector_results=(missing_main,)))

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["required_selector_missing"]


def test_zero_sized_required_selector_fails() -> None:
    evidence = healthy_evidence()
    collapsed_main = replace(
        evidence.required_selector_results[0],
        width=0,
        height=0,
    )

    result = evaluate_browser_evidence(
        replace(evidence, required_selector_results=(collapsed_main,))
    )

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["required_selector_zero_geometry"]


def test_forbidden_wordpress_error_text_fails() -> None:
    evidence = replace(
        healthy_evidence(),
        forbidden_text_matches=(
            "Fatal error: Uncaught Error in wp-content/plugins/example/plugin.php",
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["forbidden_text_match"]


def test_uncaught_page_exception_fails() -> None:
    evidence = replace(
        healthy_evidence(),
        page_exceptions=("TypeError: Cannot read properties of undefined",),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["page_exception"]


def test_critical_resource_failure_fails() -> None:
    evidence = replace(
        healthy_evidence(),
        resource_failures=(
            ResourceFailure(
                url="https://example.com/assets/application.js",
                status_code=503,
                resource_type="script",
                critical=True,
            ),
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["critical_resource_failure"]


def test_noncritical_resource_failure_is_informational() -> None:
    evidence = replace(
        healthy_evidence(),
        resource_failures=(
            ResourceFailure(
                url="https://example.com/analytics.gif",
                status_code=None,
                resource_type="image",
                critical=False,
                error="blocked by client",
            ),
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is True
    assert result.failures == ()
    assert [finding.code for finding in result.information] == ["noncritical_resource_failure"]


def test_resource_type_is_not_copied_into_finding_messages() -> None:
    evidence = replace(
        healthy_evidence(),
        resource_failures=(
            ResourceFailure(
                url="https://example.com/assets/application.js",
                status_code=503,
                resource_type="RESOURCE_TYPE_SENTINEL",
                critical=True,
            ),
        ),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert "RESOURCE_TYPE_SENTINEL" not in result.failures[0].message


def test_missing_required_text_fails() -> None:
    evidence = replace(
        healthy_evidence(),
        required_text_results=(TextResult(value="Welcome", found=False),),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["required_text_missing"]


def test_invisible_required_selector_fails() -> None:
    evidence = healthy_evidence()
    hidden_main = replace(
        evidence.required_selector_results[0],
        visible=False,
    )

    result = evaluate_browser_evidence(replace(evidence, required_selector_results=(hidden_main,)))

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["required_selector_not_visible"]


def test_console_error_fails() -> None:
    evidence = replace(
        healthy_evidence(),
        console_errors=("ReferenceError: application is not defined",),
    )

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["console_error"]
    assert "application is not defined" not in result.failures[0].message


@pytest.mark.parametrize("document_status", [None, 500])
def test_document_failure_fails(document_status: int | None) -> None:
    evidence = replace(healthy_evidence(), document_status=document_status)

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["document_failure"]


def test_browser_timeout_fails() -> None:
    evidence = replace(healthy_evidence(), timed_out=True)

    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["browser_timeout"]


@pytest.mark.parametrize(
    "evidence",
    [
        replace(
            healthy_evidence(),
            viewport=replace(
                healthy_evidence().viewport,
                device_scale_factor=10**10000,
            ),
        ),
        replace(
            healthy_evidence(),
            viewport=replace(healthy_evidence().viewport, width=10**10000),
        ),
        replace(
            healthy_evidence(),
            viewport=replace(healthy_evidence().viewport, height=10**10000),
        ),
        replace(healthy_evidence(), duration_ms=10**10000),
        replace(
            healthy_evidence(),
            screenshot=ScreenshotArtifact(
                path="artifacts/home-desktop.png",
                sha256="a" * 64,
                width=10**10000,
                height=900,
            ),
        ),
        replace(
            healthy_evidence(),
            screenshot=ScreenshotArtifact(
                path="artifacts/home-desktop.png",
                sha256="a" * 64,
                width=1440,
                height=10**10000,
            ),
        ),
        replace(
            healthy_evidence(),
            required_selector_results=(
                replace(
                    healthy_evidence().required_selector_results[0],
                    width=float("nan"),
                ),
            ),
        ),
        replace(healthy_evidence(), duration_ms=-1),
    ],
)
def test_malformed_browser_evidence_fails_closed(evidence: BrowserEvidence) -> None:
    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["invalid_browser_evidence"]


@pytest.mark.parametrize(
    "evidence",
    [
        replace(
            healthy_evidence(),
            required_text_results=(TextResult(value="Welcome", found=cast(bool, "false")),),
        ),
        replace(
            healthy_evidence(),
            required_selector_results=(cast(SelectorResult, "not-a-selector-result"),),
        ),
        replace(
            healthy_evidence(),
            resource_failures=(
                ResourceFailure(
                    url="https://example.com/application.js",
                    status_code=503,
                    resource_type="script",
                    critical=cast(bool, ""),
                ),
            ),
        ),
        replace(
            healthy_evidence(),
            interaction_results=(InteractionResult(action="click", selector="", succeeded=True),),
        ),
    ],
)
def test_malformed_nested_browser_evidence_fails_closed(
    evidence: BrowserEvidence,
) -> None:
    result = evaluate_browser_evidence(evidence)

    assert result.healthy is False
    assert [finding.code for finding in result.failures] == ["invalid_browser_evidence"]
