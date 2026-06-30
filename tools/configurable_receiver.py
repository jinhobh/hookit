"""Configurable webhook receiver for reliability demo scenarios.

A sibling to demo_receiver.py that lets callers control the HTTP status code
returned, inject artificial latency, and flip from 500→200 after a fixed number
of requests — all without touching the main application or worker code.

Signature verification is off by default so demos focus on delivery mechanics.
Pass ``--verify --secret <s>`` to opt in.

Usage examples::

    # Always return 200 (default — verify signatures):
    python tools/configurable_receiver.py --port 8891 --verify --secret <s>

    # Always return 500 (trigger retries):
    python tools/configurable_receiver.py --port 8891 --status 500

    # Fail first 3 requests with 500, then return 200 (redrive demo):
    python tools/configurable_receiver.py --port 8892 --fail-count 3

    # Add 5-second latency to let you kill the worker mid-flight:
    python tools/configurable_receiver.py --port 8894 --latency 5
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import threading
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger("configurable_receiver")

_MAX_AGE_SECONDS = 300


def _verify_signature(
    secret: str,
    signature_header: str | None,
    body: bytes,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Verify an inbound X-Webhook-Signature header (mirrors demo_receiver logic)."""
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
    if abs(current_time - timestamp) > _MAX_AGE_SECONDS:
        return False, f"timestamp too old: age={(current_time - timestamp):.0f}s"

    canonical = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_sig):
        return False, "signature mismatch"
    return True, "ok"


def make_app(
    *,
    status: int = 200,
    latency: float = 0.0,
    fail_count: int = 0,
    verify: bool = False,
    secret: str = "",
) -> Starlette:
    """Build and return the configured Starlette ASGI app.

    Args:
        status: HTTP status code to return for non-failing requests.
        latency: Seconds of artificial delay before responding.
        fail_count: Return 500 for the first *fail_count* requests, then *status*.
                    0 (default) means always return *status*.
        verify: When True, validate the X-Webhook-Signature header.
        secret: Signing secret used when *verify* is True.
    """
    counter: dict[str, int] = {"n": 0}
    lock = threading.Lock()

    async def receive_webhook(request: Request) -> JSONResponse:
        body = await request.body()

        if verify and secret:
            sig_header = request.headers.get("x-webhook-signature")
            ok, reason = _verify_signature(secret, sig_header, body)
            if not ok:
                logger.warning("sig verification failed: %s", reason)
                return JSONResponse({"error": reason}, status_code=401)

        if latency > 0:
            await asyncio.sleep(latency)

        with lock:
            counter["n"] += 1
            n = counter["n"]

        response_status = 500 if (fail_count > 0 and n <= fail_count) else status

        event_type = "unknown"
        with contextlib.suppress(json.JSONDecodeError, AttributeError):
            event_type = str(json.loads(body).get("type", "unknown"))

        logger.info("request #%d  type=%s  -> HTTP %d", n, event_type, response_status)

        if response_status < 400:
            return JSONResponse({"status": "accepted"}, status_code=response_status)
        return JSONResponse({"status": "error"}, status_code=response_status)

    return Starlette(routes=[Route("/", receive_webhook, methods=["POST"])])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="Configurable demo webhook receiver")
    parser.add_argument("--port", type=int, default=8889, help="Port to listen on (default: 8889)")
    parser.add_argument(
        "--status", type=int, default=200, help="HTTP status code to return (default: 200)"
    )
    parser.add_argument(
        "--latency", type=float, default=0.0, help="Artificial delay in seconds (default: 0)"
    )
    parser.add_argument(
        "--fail-count",
        type=int,
        default=0,
        metavar="N",
        help="Return HTTP 500 for the first N requests, then --status (default: 0 = disabled)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify HMAC-SHA256 webhook signatures (requires --secret)",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("WEBHOOK_SECRET", ""),
        help="Signing secret (required when --verify is set; or set WEBHOOK_SECRET)",
    )
    args = parser.parse_args()

    if args.verify and not args.secret:
        parser.error("--secret or WEBHOOK_SECRET env var is required when --verify is set")

    logger.info(
        "Listening on :%d  status=%d  latency=%.1fs  fail_count=%d  verify=%s",
        args.port,
        args.status,
        args.latency,
        args.fail_count,
        args.verify,
    )
    uvicorn.run(
        make_app(
            status=args.status,
            latency=args.latency,
            fail_count=args.fail_count,
            verify=args.verify,
            secret=args.secret,
        ),
        host="0.0.0.0",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
