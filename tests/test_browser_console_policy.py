from dataclasses import replace

from triage_agent.browser_checks import (
    BrowserEvidence,
    ScreenshotArtifact,
    SelectorResult,
    TextResult,
    Viewport,
    evaluate_browser_evidence,
)


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
        browser_version="120.0",
        required_text_results=(TextResult(value="Welcome", found=True),),
        required_selector_results=(
            SelectorResult(selector="main", found=True, visible=True, width=1200, height=700),
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


def test_console_errors_are_informational_when_manifest_explicitly_allows_them() -> None:
    evidence = replace(healthy_evidence(), console_errors=("console_error",))

    evaluation = evaluate_browser_evidence(evidence, ignore_console_errors=True)

    assert evaluation.healthy is True
    assert [finding.code for finding in evaluation.failures] == []
    assert [finding.code for finding in evaluation.information] == ["console_error"]


def test_console_errors_remain_failures_by_default() -> None:
    evidence = replace(healthy_evidence(), console_errors=("console_error",))

    evaluation = evaluate_browser_evidence(evidence)

    assert evaluation.healthy is False
    assert [finding.code for finding in evaluation.failures] == ["console_error"]
