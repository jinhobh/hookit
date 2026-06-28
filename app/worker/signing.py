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


def sign_payload(secret: str, timestamp: int, body: bytes) -> str:
    """Return HMAC-SHA256 hex digest of ``{timestamp}.{body}`` keyed with *secret*."""
    canonical = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def build_signature_header(secret: str, timestamp: int, body: bytes) -> str:
    """Return the ``X-Webhook-Signature`` header value: ``t={timestamp},v1={sig}``."""
    sig = sign_payload(secret, timestamp, body)
    return f"t={timestamp},v1={sig}"
