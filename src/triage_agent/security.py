import ipaddress
from urllib.parse import SplitResult, urlsplit


class UnsafeTargetError(ValueError):
    pass


def validate_probe_url(
    url: str,
    *,
    allowed_hosts: set[str],
) -> SplitResult:
    try:
        url.encode("utf-8")
        if any(character.isascii() and not character.isprintable() for character in url):
            raise ValueError("URL contains a non-printable character")
        parsed = urlsplit(url)
        port = parsed.port
    except (UnicodeEncodeError, ValueError) as exc:
        raise UnsafeTargetError("Target URL is malformed") from exc

    host = (parsed.hostname or "").lower()
    normalized_allowed_hosts = {allowed_host.lower() for allowed_host in allowed_hosts}
    if parsed.scheme != "https" or host not in normalized_allowed_hosts:
        raise UnsafeTargetError("Target must use HTTPS and be present on the allowlist")
    if parsed.username is not None or parsed.password is not None or port not in (None, 443):
        raise UnsafeTargetError("Target cannot contain credentials or nonstandard ports")
    return parsed


def validate_resolved_addresses(resolved_addresses: set[str]) -> None:
    try:
        addresses = [ipaddress.ip_address(address) for address in resolved_addresses]
    except ValueError as exc:
        raise UnsafeTargetError("Target returned an invalid address") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeTargetError("Target must resolve exclusively to a public address")


def validate_probe_target(
    url: str,
    *,
    allowed_hosts: set[str],
    resolved_addresses: set[str],
) -> str:
    validate_probe_url(url, allowed_hosts=allowed_hosts)
    validate_resolved_addresses(resolved_addresses)
    return url
