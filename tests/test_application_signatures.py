import pytest

from triage_agent.application_signatures import detect_application_failure_codes


@pytest.mark.parametrize(
    ("visible_text", "expected_code"),
    [
        (
            "There has been a critical error on this website. Please check your inbox.",
            "wordpress_critical_error",
        ),
        (
            "Fatal error: Uncaught TypeError in /var/www/html/index.php on line 42",
            "php_fatal_error",
        ),
        (
            "Fatal error: Uncaught Error: boom in /var/www/index.php:12\nStack trace:\n"
            "#0 {main}\n thrown in /var/www/index.php on line 12",
            "php_fatal_error",
        ),
        (
            "Parse error: syntax error, unexpected token in /var/www/plugin.php on line 7",
            "php_parse_error",
        ),
        (
            "Uncaught RuntimeException: failed in /var/www/html/wp-content/plugin.php:19",
            "php_uncaught_exception",
        ),
        ("Uncaught Exception: boom in /var/www/index.php:12", "php_uncaught_exception"),
        ("Uncaught Error: boom in /var/www/index.php:12", "php_uncaught_exception"),
        ("Error establishing a database connection", "wordpress_database_connection_failure"),
        (
            "Briefly unavailable for scheduled maintenance. Check back in a minute.",
            "wordpress_maintenance_mode",
        ),
    ],
)
def test_detects_narrow_visible_application_failures(
    visible_text: str,
    expected_code: str,
) -> None:
    assert expected_code in detect_application_failure_codes(visible_text)


@pytest.mark.parametrize(
    "visible_text",
    [
        "Our error handling guide explains how to debug PHP applications.",
        "The phrase Fatal error appears here as documentation without a PHP location.",
        "Use [example] when writing documentation.",
        "The database connection is monitored continuously.",
        "There was an error processing your request.",
    ],
)
def test_avoids_broad_error_and_documentation_false_positives(visible_text: str) -> None:
    assert detect_application_failure_codes(visible_text) == ()


def test_deduplicates_signatures_and_never_returns_visible_error_text() -> None:
    visible_text = "\n".join(
        [
            "There has been a critical error on this website.",
            "There has been a critical error on this website.",
        ]
    )

    assert detect_application_failure_codes(visible_text) == ("wordpress_critical_error",)


def test_unrendered_shortcode_detection_requires_explicit_allowlist() -> None:
    text = '[contact-form-7 id="123" title="Contact"]'

    assert detect_application_failure_codes(text) == ()
    assert detect_application_failure_codes(text, shortcode_names=("contact-form-7",)) == (
        "wordpress_unrendered_shortcode",
    )


def test_signature_scan_is_bounded_to_visible_text_limit() -> None:
    oversized_prefix = "x" * 1_000_001

    assert detect_application_failure_codes(
        oversized_prefix + "There has been a critical error on this website."
    ) == ()
