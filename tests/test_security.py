import pytest

from triage_agent.security import UnsafeTargetError, validate_probe_target


def test_allows_https_target_on_explicit_allowlist() -> None:
    target = validate_probe_target(
        "https://monitor.example.com/health",
        allowed_hosts={"monitor.example.com"},
        resolved_addresses={"104.21.10.20"},
    )

    assert target == "https://monitor.example.com/health"


def test_rejects_private_address_even_when_host_is_allowlisted() -> None:
    with pytest.raises(UnsafeTargetError, match="public address"):
        validate_probe_target(
            "https://monitor.example.com/",
            allowed_hosts={"monitor.example.com"},
            resolved_addresses={"127.0.0.1"},
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@monitor.example.com/",
        "https://monitor.example.com:8443/",
    ],
)
def test_rejects_credentials_and_nonstandard_ports(url: str) -> None:
    with pytest.raises(UnsafeTargetError, match="credentials or nonstandard ports"):
        validate_probe_target(
            url,
            allowed_hosts={"monitor.example.com"},
            resolved_addresses={"104.21.10.20"},
        )
