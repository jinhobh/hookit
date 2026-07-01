"""HMAC-SHA256 signing utilities for outbound webhook deliveries.

Canonical string: ``{timestamp}.{body_bytes}``

The receiver verifies by:
  1. Parsing ``t`` and ``v1`` from ``X-Webhook-Signature``.
  2. Recomputing HMAC over ``f"{t}.{raw_body}".encode()`` with the shared secret.
  3. Using a constant-time comparison.
"""

from __future__ import annotations

import hashlib
import hmac
import time

_MAX_AGE_SECONDS = 300  # 5 minutes replay window


def sign_payload(secret: str, timestamp: int, body: bytes) -> str:
    """Return HMAC-SHA256 hex digest of ``{timestamp}.{body}`` keyed with *secret*."""
    canonical = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def build_signature_header(secret: str, timestamp: int, body: bytes) -> str:
    """Return the ``X-Webhook-Signature`` header value: ``t={timestamp},v1={sig}``."""
    sig = sign_payload(secret, timestamp, body)
    return f"t={timestamp},v1={sig}"


def verify_signature(
    secret: str,
    signature_header: str | None,
    body: bytes,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Verify an inbound ``X-Webhook-Signature`` header.

    Returns ``(True, "ok")`` on success or ``(False, reason)`` on failure.
    ``now`` is injectable for testing (defaults to ``time.time()``). Mirrors
    the reference implementation in ``tools/demo_receiver.py``, which real
    integrators can copy-paste standalone; this copy is for the in-app
    ``/simulate/receiver`` endpoint.
    """
    if not signature_header:
        return False, "missing signature header"

    parts: dict[str, str] = {}
    for part in signature_header.split(","):
        if "=" in part:
            key, _, value = part.partition("=")
            parts[key.strip()] = value.strip()

    timestamp_str = parts.get("t")
    received_sig = parts.get("v1")
    if not timestamp_str or not received_sig:
        return False, "malformed signature header"

    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return False, "invalid timestamp in header"

    current_time = now if now is not None else time.time()
    age = current_time - timestamp
    if abs(age) > _MAX_AGE_SECONDS:
        return False, f"timestamp too old: age={age:.0f}s exceeds {_MAX_AGE_SECONDS}s"

    expected = sign_payload(secret, timestamp, body)
    if not hmac.compare_digest(expected, received_sig):
        return False, "signature mismatch"

    return True, "ok"
