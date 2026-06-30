"""Scenario 1: Failure → Exponential Backoff → Dead-letter.

Demonstrates:
- A webhook endpoint that always returns HTTP 500 drives a delivery through
  the full retry cycle until MAX_DELIVERY_ATTEMPTS is exhausted.
- Each failed attempt produces a DeliveryAttempt row with the response status
  and duration.
- next_attempt_at advances according to exponential backoff (base=3s, 2× each
  attempt) so the timing is observable.
- After the final attempt the delivery transitions to DEAD_LETTERED.

Run from the project root::

    python -m demo.scenario_1_failure_backoff_deadletter
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo._shared import (  # noqa: E402
    API_BASE_URL,
    WORKER_DEMO_ENV,
    create_endpoint,
    divider,
    failed,
    get_delivery,
    info,
    list_attempts,
    passed,
    post_event,
    print_delivery_summary,
    section,
    setup_project,
    start_receiver,
    start_worker,
    step,
    stop_proc,
)

_LABEL = "Scenario 1 – Failure → Backoff → Dead-letter"
_RECEIVER_PORT = 8891


def run() -> None:
    section(_LABEL)
    info(f"API: {API_BASE_URL}")
    info(
        f"Worker env: MAX_DELIVERY_ATTEMPTS={WORKER_DEMO_ENV['MAX_DELIVERY_ATTEMPTS']} "
        f"RETRY_BASE_SECONDS={WORKER_DEMO_ENV['RETRY_BASE_SECONDS']}s"
    )

    # ── Setup ────────────────────────────────────────────────────────────────
    divider("Setup")
    receiver = start_receiver(_RECEIVER_PORT, status=500)
    step(f"receiver pid={receiver.pid} → always HTTP 500 on port {_RECEIVER_PORT}")

    _project_id, api_key = setup_project("demo-s1")
    endpoint_url = f"http://localhost:{_RECEIVER_PORT}/"
    create_endpoint(api_key, endpoint_url, ["order.created"])

    # ── Send event ───────────────────────────────────────────────────────────
    divider("Send event")
    result = post_event(api_key, "order.created", {"order_id": "ord-001", "amount": 99})
    event_id: str = str(result["event_id"])

    from demo._shared import get_event  # noqa: E402  (avoid circular at module level)

    event_detail = get_event(api_key, event_id)
    if not event_detail["deliveries"]:
        failed(_LABEL, "no delivery was queued for the event")
        return
    delivery_id: str = str(event_detail["deliveries"][0]["id"])
    step(f"delivery_id {delivery_id[:8]}…")

    # ── Start worker ─────────────────────────────────────────────────────────
    divider("Worker processing")
    worker = start_worker()
    step(f"worker pid={worker.pid}")

    # Poll: watch attempt_count rise, printing each new attempt as it lands.
    max_attempts = int(WORKER_DEMO_ENV["MAX_DELIVERY_ATTEMPTS"])
    seen_count = 0
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        d = get_delivery(api_key, delivery_id)
        if d["attempt_count"] > seen_count:
            attempts = list_attempts(api_key, delivery_id)
            for a in attempts[seen_count:]:
                http = a.get("response_status") or "err"
                ms = a.get("duration_ms") or "?"
                next_at = d.get("next_attempt_at", "")
                info(
                    f"attempt #{a['attempt_number']}  HTTP {http}  {ms}ms"
                    + (f"  (next retry ≥ {next_at})" if d["status"] == "pending" else "")
                )
            seen_count = d["attempt_count"]
        if d["status"] == "dead_lettered":
            step(f"delivery reached DEAD_LETTERED after {seen_count}/{max_attempts} attempts ✓")
            break
        time.sleep(0.5)
    else:
        failed(_LABEL, "delivery did not reach dead_lettered within timeout")
        return

    # ── Final state ──────────────────────────────────────────────────────────
    divider("Final delivery state")
    d = get_delivery(api_key, delivery_id)
    attempts = list_attempts(api_key, delivery_id)
    print_delivery_summary(d, attempts)

    # ── Verify acceptance criteria ───────────────────────────────────────────
    assert d["status"] == "dead_lettered", f"expected dead_lettered, got {d['status']}"
    assert d["attempt_count"] == max_attempts, (
        f"expected {max_attempts} attempts, got {d['attempt_count']}"
    )
    assert len(attempts) == max_attempts, (
        f"expected {max_attempts} attempt rows, got {len(attempts)}"
    )
    assert all(a["response_status"] == 500 for a in attempts), "not all attempts returned 500"

    # ── Teardown ─────────────────────────────────────────────────────────────
    stop_proc(worker, "worker")
    stop_proc(receiver, "receiver")

    passed(_LABEL)


if __name__ == "__main__":
    run()
