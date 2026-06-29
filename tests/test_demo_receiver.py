"""Unit tests for the demo_receiver standalone HMAC verification logic."""

from __future__ import annotations

import hashlib
import hmac
import time

from tools.demo_receiver import verify_signature

_SECRET = "test-secret-key"
_BODY = b'{"event_type": "order.created", "id": "evt_123"}'


def _make_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Build a valid X-Webhook-Signature header value for the given inputs."""
    ts = timestamp if timestamp is not None else int(time.time())
    canonical = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_valid_signature_passes() -> None:
    now = time.time()
    ts = int(now)
    header = _make_header(_SECRET, _BODY, ts)
    ok, reason = verify_signature(_SECRET, header, _BODY, now=now)
    assert ok, reason


def test_wrong_secret_fails() -> None:
    header = _make_header("wrong-secret", _BODY)
    ok, reason = verify_signature(_SECRET, header, _BODY)
    assert not ok
    assert "mismatch" in reason


def test_tampered_body_fails() -> None:
    now = time.time()
    ts = int(now)
    header = _make_header(_SECRET, _BODY, ts)
    ok, reason = verify_signature(_SECRET, header, b'{"event_type": "tampered"}', now=now)
    assert not ok
    assert "mismatch" in reason


def test_expired_timestamp_fails() -> None:
    old_ts = int(time.time()) - 400  # 400 s ago — exceeds the 5-minute window
    header = _make_header(_SECRET, _BODY, old_ts)
    ok, reason = verify_signature(_SECRET, header, _BODY)
    assert not ok
    assert "old" in reason


def test_missing_header_fails() -> None:
    ok, reason = verify_signature(_SECRET, None, _BODY)
    assert not ok
    assert "missing" in reason


def test_malformed_header_fails() -> None:
    ok, reason = verify_signature(_SECRET, "not-a-valid-header", _BODY)
    assert not ok
    assert "malformed" in reason


def test_future_timestamp_within_window_passes() -> None:
    now = time.time()
    future_ts = int(now) + 60  # 1 minute in the future — within the 5-minute window
    header = _make_header(_SECRET, _BODY, future_ts)
    ok, reason = verify_signature(_SECRET, header, _BODY, now=now)
    assert ok, reason
