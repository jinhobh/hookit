"""Standalone demo webhook receiver with HMAC-SHA256 signature verification.

Usage:
    python tools/demo_receiver.py --secret <secret> [--port 8888]
    WEBHOOK_SECRET=<secret> python tools/demo_receiver.py

POST any request to / — returns 200 on valid signature, 401 otherwise.
The verification logic mirrors the signing format in app/worker/signing.py:
    canonical = f"{timestamp}.".encode() + body_bytes
    signature = HMAC-SHA256(secret, canonical).hexdigest()
    header = f"t={timestamp},v1={signature}"
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger("demo_receiver")

_MAX_AGE_SECONDS = 300  # 5 minutes replay window


def verify_signature(
    secret: str,
    signature_header: str | None,
    body: bytes,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Verify an inbound X-Webhook-Signature header.

    Returns ``(True, "ok")`` on success or ``(False, reason)`` on failure.
    The ``now`` parameter is injectable for testing (defaults to ``time.time()``).
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

    canonical = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_sig):
        return False, "signature mismatch"

    return True, "ok"


def _parse_timestamp_age(signature_header: str) -> str:
    """Extract and format timestamp age for logging; returns empty string on failure."""
    for part in signature_header.split(","):
        if part.startswith("t="):
            try:
                ts = int(part[2:])
                return f" age={time.time() - ts:.0f}s"
            except ValueError:
                return ""
    return ""


def make_app(secret: str) -> Starlette:
    """Create and return the Starlette ASGI application."""

    async def receive_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        sig_header = request.headers.get("x-webhook-signature")

        valid, reason = verify_signature(secret, sig_header, body)

        event_type = "unknown"
        try:
            payload = json.loads(body)
            event_type = str(payload.get("event_type", "unknown"))
        except (json.JSONDecodeError, AttributeError):
            pass

        age_info = _parse_timestamp_age(sig_header) if sig_header else ""
        outcome = "PASS" if valid else f"FAIL ({reason})"
        logger.info(
            "path=%s event_type=%s%s verification=%s",
            request.url.path,
            event_type,
            age_info,
            outcome,
        )

        if not valid:
            return JSONResponse({"error": reason}, status_code=401)

        return JSONResponse({"status": "accepted"}, status_code=200)

    return Starlette(routes=[Route("/", receive_webhook, methods=["POST"])])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="Demo webhook receiver with HMAC verification")
    parser.add_argument(
        "--secret",
        default=os.environ.get("WEBHOOK_SECRET", ""),
        help="Webhook signing secret (or set WEBHOOK_SECRET env var)",
    )
    parser.add_argument("--port", type=int, default=8888, help="Port to listen on (default: 8888)")
    args = parser.parse_args()

    if not args.secret:
        parser.error("--secret or WEBHOOK_SECRET env var is required")

    logger.info("Starting demo receiver on port %d", args.port)
    uvicorn.run(make_app(args.secret), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
