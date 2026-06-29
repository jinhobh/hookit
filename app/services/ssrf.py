"""SSRF protection: validate that a webhook URL targets a public IP address."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class SSRFError(ValueError):
    """Raised when a URL targets a non-public (blocked) IP address."""


_BLOCKED_V4_NETWORKS: list[ipaddress.IPv4Network] = [
    ipaddress.IPv4Network("127.0.0.0/8"),  # loopback
    ipaddress.IPv4Network("10.0.0.0/8"),  # RFC 1918 private
    ipaddress.IPv4Network("172.16.0.0/12"),  # RFC 1918 private
    ipaddress.IPv4Network("192.168.0.0/16"),  # RFC 1918 private
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local / instance metadata
]

_BLOCKED_V6_NETWORKS: list[ipaddress.IPv6Network] = [
    ipaddress.IPv6Network("::1/128"),  # loopback
    ipaddress.IPv6Network("fe80::/10"),  # link-local
]

_SSRF_ERROR_MSG = "Webhook URL targets a non-public address and cannot be registered"


def validate_url_not_ssrf(url: str) -> None:
    """Raise SSRFError if *url* is an IP literal targeting a blocked address range.

    Only IP-literal hostnames are checked.  Hostname-based URLs are not
    DNS-resolved — DNS-based SSRF is a known limitation and out of scope.

    Raises:
        SSRFError: if the URL's host is a blocked IP literal.
        ValueError: if the URL cannot be parsed.
    """
    parsed = urlparse(url)
    host = parsed.hostname  # strips [] for IPv6 literals; lowercases
    if not host:
        raise ValueError(f"Could not parse host from URL: {url!r}")

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP literal — hostname-based URL, DNS resolution out of scope
        return

    blocked: bool
    if isinstance(addr, ipaddress.IPv4Address):
        blocked = addr == ipaddress.IPv4Address("0.0.0.0") or any(
            addr in net for net in _BLOCKED_V4_NETWORKS
        )
    else:
        blocked = any(addr in net for net in _BLOCKED_V6_NETWORKS)
    if blocked:
        raise SSRFError(_SSRF_ERROR_MSG)
