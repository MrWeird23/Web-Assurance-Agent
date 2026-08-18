from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class Viewport:
    width: int
    height: int
    device_scale_factor: float


@dataclass(frozen=True, slots=True)
class TextResult:
    value: str
    found: bool


@dataclass(frozen=True, slots=True)
class SelectorResult:
    selector: str
    found: bool
    visible: bool
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class PluginAssertionResult:
    assertion_id: str
    kind: str
    satisfied: bool


@dataclass(frozen=True, slots=True)
class InteractionResult:
    action: str
    selector: str
    succeeded: bool


@dataclass(frozen=True, slots=True)
class ResourceFailure:
    url: str
    status_code: int | None
    resource_type: str
    critical: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ScreenshotArtifact:
    path: str
    sha256: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class BrowserEvidence:
    page_id: str
    requested_url: str
    final_url: str
    viewport: Viewport
    page_width: int
    page_height: int
    device_profile: str
    document_status: int | None
    title: str
    browser_version: str
    required_text_results: tuple[TextResult, ...]
    required_selector_results: tuple[SelectorResult, ...]
    forbidden_text_matches: tuple[str, ...]
    application_failure_codes: tuple[str, ...]
    plugin_assertion_results: tuple[PluginAssertionResult, ...]
    interaction_results: tuple[InteractionResult, ...]
    console_errors: tuple[str, ...]
    page_exceptions: tuple[str, ...]
    resource_failures: tuple[ResourceFailure, ...]
    duration_ms: int
    timed_out: bool
    screenshot: ScreenshotArtifact | None


@dataclass(frozen=True, slots=True)
class BrowserFinding:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BrowserEvaluation:
    healthy: bool
    failures: tuple[BrowserFinding, ...]
    information: tuple[BrowserFinding, ...]


_MAX_DIMENSION_PX = 32_768
_MAX_DURATION_MS = 86_400_000
_MAX_GEOMETRY_PX = 1_000_000.0
_MAX_DEVICE_SCALE_FACTOR = 16.0
APPLICATION_FAILURE_CODES = frozenset(
    {
        "wordpress_critical_error",
        "php_fatal_error",
        "php_parse_error",
        "php_uncaught_exception",
        "wordpress_database_connection_failure",
        "wordpress_maintenance_mode",
        "wordpress_unrendered_shortcode",
    }
)
PLUGIN_ASSERTION_KINDS = frozenset(
    {
        "elementor",
        "contact-form-7",
        "woocommerce",
        "gallery-slider",
        "search",
        "multilingual",
    }
)
INTERACTION_ACTIONS = frozenset({"click", "fill"})


def _is_bounded_int(value: object, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _is_finite_number(value: object, *, minimum: float, maximum: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric_value = float(value)
    except OverflowError:
        return False
    return isfinite(numeric_value) and minimum <= numeric_value <= maximum


def _is_valid_evidence(evidence: BrowserEvidence) -> bool:
    if type(evidence) is not BrowserEvidence:
        return False
    if any(
        type(value) is not str
        for value in (
            evidence.page_id,
            evidence.requested_url,
            evidence.final_url,
            evidence.device_profile,
            evidence.title,
            evidence.browser_version,
        )
    ):
        return False
    if type(evidence.timed_out) is not bool:
        return False
    if any(
        type(values) is not tuple
        for values in (
            evidence.required_text_results,
            evidence.required_selector_results,
            evidence.forbidden_text_matches,
            evidence.application_failure_codes,
            evidence.plugin_assertion_results,
            evidence.interaction_results,
            evidence.console_errors,
            evidence.page_exceptions,
            evidence.resource_failures,
        )
    ):
        return False

    viewport = evidence.viewport
    if type(viewport) is not Viewport or not (
        _is_bounded_int(viewport.width, minimum=1, maximum=_MAX_DIMENSION_PX)
        and _is_bounded_int(viewport.height, minimum=1, maximum=_MAX_DIMENSION_PX)
        and _is_bounded_int(evidence.page_width, minimum=0, maximum=int(_MAX_GEOMETRY_PX))
        and _is_bounded_int(evidence.page_height, minimum=0, maximum=int(_MAX_GEOMETRY_PX))
        and _is_finite_number(
            viewport.device_scale_factor,
            minimum=0.25,
            maximum=_MAX_DEVICE_SCALE_FACTOR,
        )
    ):
        return False
    if not _is_bounded_int(
        evidence.duration_ms,
        minimum=0,
        maximum=_MAX_DURATION_MS,
    ):
        return False
    if evidence.document_status is not None and not (
        _is_bounded_int(evidence.document_status, minimum=100, maximum=599)
    ):
        return False

    if any(
        type(result) is not TextResult
        or type(result.value) is not str
        or type(result.found) is not bool
        for result in evidence.required_text_results
    ):
        return False
    if any(
        type(result) is not SelectorResult
        or type(result.selector) is not str
        or type(result.found) is not bool
        or type(result.visible) is not bool
        or not _is_finite_number(
            result.width,
            minimum=0,
            maximum=_MAX_GEOMETRY_PX,
        )
        or not _is_finite_number(
            result.height,
            minimum=0,
            maximum=_MAX_GEOMETRY_PX,
        )
        for result in evidence.required_selector_results
    ):
        return False
    if any(
        type(value) is not str
        for value in (
            *evidence.forbidden_text_matches,
            *evidence.application_failure_codes,
            *evidence.console_errors,
            *evidence.page_exceptions,
        )
    ):
        return False
    if any(code not in APPLICATION_FAILURE_CODES for code in evidence.application_failure_codes):
        return False
    if any(
        type(result) is not PluginAssertionResult
        or type(result.assertion_id) is not str
        or not result.assertion_id
        or type(result.kind) is not str
        or result.kind not in PLUGIN_ASSERTION_KINDS
        or type(result.satisfied) is not bool
        for result in evidence.plugin_assertion_results
    ):
        return False
    if any(
        type(result) is not InteractionResult
        or type(result.action) is not str
        or result.action not in INTERACTION_ACTIONS
        or type(result.selector) is not str
        or not result.selector
        or type(result.succeeded) is not bool
        for result in evidence.interaction_results
    ):
        return False
    if any(
        type(resource) is not ResourceFailure
        or type(resource.url) is not str
        or type(resource.resource_type) is not str
        or type(resource.critical) is not bool
        or (resource.error is not None and type(resource.error) is not str)
        or (
            resource.status_code is not None
            and not (_is_bounded_int(resource.status_code, minimum=100, maximum=599))
        )
        for resource in evidence.resource_failures
    ):
        return False
    if evidence.screenshot is not None:
        screenshot = evidence.screenshot
        if type(screenshot) is not ScreenshotArtifact or not (
            type(screenshot.path) is str
            and bool(screenshot.path)
            and type(screenshot.sha256) is str
            and _is_bounded_int(
                screenshot.width,
                minimum=1,
                maximum=_MAX_DIMENSION_PX,
            )
            and _is_bounded_int(
                screenshot.height,
                minimum=1,
                maximum=_MAX_DIMENSION_PX,
            )
            and len(screenshot.sha256) == 64
            and all(character in "0123456789abcdef" for character in screenshot.sha256)
        ):
            return False
    return True


def evaluate_browser_evidence(evidence: BrowserEvidence) -> BrowserEvaluation:
    if not _is_valid_evidence(evidence):
        return BrowserEvaluation(
            healthy=False,
            failures=(
                BrowserFinding(
                    code="invalid_browser_evidence",
                    message="The browser collector returned invalid evidence.",
                ),
            ),
            information=(),
        )

    failures: list[BrowserFinding] = []
    if evidence.timed_out:
        failures.append(
            BrowserFinding(
                code="browser_timeout",
                message="The browser check exceeded its time limit.",
            )
        )
    if evidence.document_status is None or not 200 <= evidence.document_status < 400:
        failures.append(
            BrowserFinding(
                code="document_failure",
                message="The browser did not receive a successful document response.",
            )
        )
    failures.extend(
        BrowserFinding(
            code="required_text_missing",
            message="Required page text was not found.",
        )
        for result in evidence.required_text_results
        if not result.found
    )
    for result in evidence.required_selector_results:
        if not result.found:
            failures.append(
                BrowserFinding(
                    code="required_selector_missing",
                    message="A required selector was not found.",
                )
            )
        elif not result.visible:
            failures.append(
                BrowserFinding(
                    code="required_selector_not_visible",
                    message="A required selector is not visible.",
                )
            )
        elif result.width <= 0 or result.height <= 0:
            failures.append(
                BrowserFinding(
                    code="required_selector_zero_geometry",
                    message="A required selector has no rendered area.",
                )
            )
    failures.extend(
        BrowserFinding(
            code="forbidden_text_match",
            message="Forbidden application error text was visible.",
        )
        for _match in evidence.forbidden_text_matches
    )
    failures.extend(
        BrowserFinding(
            code="application_failure",
            message=f"Detected application failure: {failure_code}",
        )
        for failure_code in evidence.application_failure_codes
    )
    failures.extend(
        BrowserFinding(
            code="plugin_assertion_failed",
            message=f"Plugin assertion failed: {result.assertion_id}",
        )
        for result in evidence.plugin_assertion_results
        if not result.satisfied
    )
    failures.extend(
        BrowserFinding(
            code="interaction_failed",
            message=f"A safe {result.action} interaction failed.",
        )
        for result in evidence.interaction_results
        if not result.succeeded
    )
    failures.extend(
        BrowserFinding(
            code="console_error",
            message="The page emitted a browser console error.",
        )
        for _error in evidence.console_errors
    )
    failures.extend(
        BrowserFinding(
            code="page_exception",
            message="The page raised an uncaught JavaScript exception.",
        )
        for _exception in evidence.page_exceptions
    )
    failures.extend(
        BrowserFinding(
            code="critical_resource_failure",
            message="A critical page resource failed to load.",
        )
        for resource in evidence.resource_failures
        if resource.critical
    )
    information = tuple(
        BrowserFinding(
            code="noncritical_resource_failure",
            message="A non-critical page resource failed to load.",
        )
        for resource in evidence.resource_failures
        if not resource.critical
    )
    return BrowserEvaluation(
        healthy=not failures,
        failures=tuple(failures),
        information=information,
    )
