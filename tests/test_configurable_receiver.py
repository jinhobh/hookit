"""Unit tests for tools/configurable_receiver.py.

Tests the ASGI app returned by make_app() using Starlette's synchronous
TestClient — no real HTTP server or subprocess is started.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from starlette.testclient import TestClient
from tools.configurable_receiver import make_app

_BODY = b'{"type":"order.created","payload":{"order_id":"ord-1"}}'
_SECRET = "test-signing-secret"


def _make_sig_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Build a valid X-Webhook-Signature header value."""
    ts = timestamp if timestamp is not None else int(time.time())
    canonical = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# ---------------------------------------------------------------------------
# Default status / basic routing
# ---------------------------------------------------------------------------


def test_default_returns_200() -> None:
    client = TestClient(make_app())
    resp = client.post("/", content=_BODY, headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_custom_status_500() -> None:
    client = TestClient(make_app(status=500))
    resp = client.post("/", content=_BODY, headers={"Content-Type": "application/json"})
    assert resp.status_code == 500
    assert resp.json()["status"] == "error"


def test_custom_status_201() -> None:
    client = TestClient(make_app(status=201))
    resp = client.post("/", content=_BODY)
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# fail_count: first N → 500, then → configured status
# ---------------------------------------------------------------------------


def test_fail_count_zero_uses_status() -> None:
    client = TestClient(make_app(status=200, fail_count=0))
    for _ in range(3):
        resp = client.post("/", content=_BODY)
        assert resp.status_code == 200


def test_fail_count_transitions_after_n_requests() -> None:
    client = TestClient(make_app(status=200, fail_count=3))

    for i in range(1, 4):
        resp = client.post("/", content=_BODY)
        assert resp.status_code == 500, f"request #{i} expected 500, got {resp.status_code}"

    resp = client.post("/", content=_BODY)
    assert resp.status_code == 200, f"request #4 expected 200, got {resp.status_code}"

    resp = client.post("/", content=_BODY)
    assert resp.status_code == 200


def test_fail_count_with_custom_success_status() -> None:
    client = TestClient(make_app(status=202, fail_count=2))
    assert client.post("/", content=_BODY).status_code == 500  # #1
    assert client.post("/", content=_BODY).status_code == 500  # #2
    assert client.post("/", content=_BODY).status_code == 202  # #3 (success)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_no_verify_accepts_unsigned_request() -> None:
    client = TestClient(make_app(verify=False))
    resp = client.post("/", content=_BODY)
    assert resp.status_code == 200


def test_verify_rejects_missing_signature() -> None:
    client = TestClient(make_app(verify=True, secret=_SECRET))
    resp = client.post("/", content=_BODY)
    assert resp.status_code == 401
    assert "missing" in resp.json()["error"]


def test_verify_rejects_bad_secret() -> None:
    client = TestClient(make_app(verify=True, secret=_SECRET))
    header = _make_sig_header("wrong-secret", _BODY)
    resp = client.post("/", content=_BODY, headers={"x-webhook-signature": header})
    assert resp.status_code == 401
    assert "mismatch" in resp.json()["error"]


def test_verify_accepts_valid_signature() -> None:
    client = TestClient(make_app(verify=True, secret=_SECRET))
    header = _make_sig_header(_SECRET, _BODY)
    resp = client.post("/", content=_BODY, headers={"x-webhook-signature": header})
    assert resp.status_code == 200


def test_verify_rejects_expired_timestamp() -> None:
    client = TestClient(make_app(verify=True, secret=_SECRET))
    old_ts = int(time.time()) - 400  # outside 5-minute window
    header = _make_sig_header(_SECRET, _BODY, timestamp=old_ts)
    resp = client.post("/", content=_BODY, headers={"x-webhook-signature": header})
    assert resp.status_code == 401
    assert "old" in resp.json()["error"]


def test_verify_accepts_valid_sig_returns_custom_status() -> None:
    client = TestClient(make_app(status=202, verify=True, secret=_SECRET))
    header = _make_sig_header(_SECRET, _BODY)
    resp = client.post("/", content=_BODY, headers={"x-webhook-signature": header})
    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# fail_count + verify interaction
# ---------------------------------------------------------------------------


def test_fail_count_and_verify_combined() -> None:
    client = TestClient(make_app(status=200, fail_count=1, verify=True, secret=_SECRET))
    header = _make_sig_header(_SECRET, _BODY)

    # request #1: valid sig, but within fail_count → 500
    resp1 = client.post("/", content=_BODY, headers={"x-webhook-signature": header})
    assert resp1.status_code == 500

    # request #2: valid sig, past fail_count → 200
    header2 = _make_sig_header(_SECRET, _BODY)
    resp2 = client.post("/", content=_BODY, headers={"x-webhook-signature": header2})
    assert resp2.status_code == 200
