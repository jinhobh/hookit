"""Unit tests for SSRF URL validation (no DB or network required)."""

from __future__ import annotations

import pytest
from app.services.ssrf import SSRFError, validate_url_not_ssrf

# ---------------------------------------------------------------------------
# Valid public URLs — must pass without raising
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/webhook",
        "http://api.example.org/hook",
        "https://hooks.example.net/events",
        "https://8.8.8.8/endpoint",  # Google public DNS
        "https://93.184.216.34/hook",  # example.com IP (public)
        "https://2606:2800:220:1:248:1893:25c8:1946/hook",  # IPv6 public
    ],
)
def test_valid_public_url_passes(url: str) -> None:
    validate_url_not_ssrf(url)  # must not raise


# ---------------------------------------------------------------------------
# Blocked IP literals — must raise SSRFError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # 127.0.0.0/8 loopback
        "http://127.0.0.1:9999/hook",
        "http://127.0.0.1/hook",
        "http://127.255.255.255/hook",
        # 10.0.0.0/8 private
        "http://10.0.0.1/hook",
        "http://10.10.20.30/hook",
        "http://10.255.255.255/hook",
        # 192.168.0.0/16 private
        "http://192.168.1.1/hook",
        "http://192.168.0.1/hook",
        # 172.16.0.0/12 private
        "http://172.16.0.1/hook",
        "http://172.31.255.255/hook",
        # 169.254.0.0/16 link-local / instance metadata
        "http://169.254.169.254/hook",
        "http://169.254.0.1/hook",
        # 0.0.0.0 unspecified
        "http://0.0.0.0/hook",
        # IPv6 loopback ::1
        "http://[::1]/hook",
        "http://[::1]:8080/hook",
        # IPv6 link-local fe80::/10
        "http://[fe80::1]/hook",
        "http://[fe80::1%25eth0]/hook",
    ],
)
def test_blocked_url_raises_ssrf_error(url: str) -> None:
    with pytest.raises(SSRFError):
        validate_url_not_ssrf(url)


# ---------------------------------------------------------------------------
# SSRFError is a subclass of ValueError
# ---------------------------------------------------------------------------


def test_ssrf_error_is_value_error() -> None:
    with pytest.raises(ValueError):
        validate_url_not_ssrf("http://127.0.0.1/hook")


# ---------------------------------------------------------------------------
# Error message is user-friendly (does not leak internal range details)
# ---------------------------------------------------------------------------


def test_ssrf_error_message_is_generic() -> None:
    with pytest.raises(SSRFError, match="non-public address"):
        validate_url_not_ssrf("http://10.0.0.1/hook")
