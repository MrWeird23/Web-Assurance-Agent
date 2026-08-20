import re

MAX_VISIBLE_TEXT_CHARS = 1_000_000
_WORDPRESS_CRITICAL_ERROR = re.compile(
    r"\bthere has been a critical error on this website\b",
    re.IGNORECASE,
)
_PHP_FATAL_ERROR = re.compile(
    r"\bfatal error:\s+(?:uncaught\s+)?[^\n]{1,300}\s+in\s+[^\n]{1,300}\.php"
    r"(?::\d+|\s+on line\s+\d+)\b",
    re.IGNORECASE,
)
_PHP_PARSE_ERROR = re.compile(
    r"\bparse error:\s+[^\n]{1,300}\s+in\s+[^\n]{1,300}\.php\s+on line\s+\d+\b",
    re.IGNORECASE,
)
_PHP_UNCAUGHT_EXCEPTION = re.compile(
    r"\buncaught\s+(?:[A-Za-z_\\][A-Za-z0-9_\\]*)?(?:exception|error):\s+[^\n]{1,300}"
    r"\s+in\s+[^\n]{1,300}\.php(?::\d+|\s+on line\s+\d+)\b",
    re.IGNORECASE,
)
_WORDPRESS_DATABASE_CONNECTION_FAILURE = re.compile(
    r"\berror establishing a database connection\b",
    re.IGNORECASE,
)
_WORDPRESS_MAINTENANCE_MODE = re.compile(
    r"\bbriefly unavailable for scheduled maintenance\.\s*check back in a minute\.?",
    re.IGNORECASE,
)

_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("wordpress_critical_error", _WORDPRESS_CRITICAL_ERROR),
    ("php_fatal_error", _PHP_FATAL_ERROR),
    ("php_parse_error", _PHP_PARSE_ERROR),
    ("php_uncaught_exception", _PHP_UNCAUGHT_EXCEPTION),
    ("wordpress_database_connection_failure", _WORDPRESS_DATABASE_CONNECTION_FAILURE),
    ("wordpress_maintenance_mode", _WORDPRESS_MAINTENANCE_MODE),
)


def detect_application_failure_codes(
    visible_text: str,
    *,
    shortcode_names: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return stable codes for narrow application failures in visible page text."""
    bounded_text = visible_text[:MAX_VISIBLE_TEXT_CHARS]
    codes = [code for code, pattern in _SIGNATURES if pattern.search(bounded_text)]
    if any(_contains_shortcode(bounded_text, name) for name in shortcode_names):
        codes.append("wordpress_unrendered_shortcode")
    return tuple(codes)


def _contains_shortcode(visible_text: str, name: str) -> bool:
    escaped_name = re.escape(name)
    pattern = re.compile(
        rf"(?<!\[)\[{escaped_name}(?=[\s\]])[^\]\r\n]{{0,500}}\](?!\])",
        re.IGNORECASE,
    )
    return pattern.search(visible_text) is not None
