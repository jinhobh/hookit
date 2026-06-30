"""Scenario 2: Redrive a dead-lettered delivery.

Demonstrates:
- A delivery that exhausted all retry attempts (DEAD_LETTERED) can be
  re-queued via POST /deliveries/{id}/redrive.
- The redrive sets status→pending and next_attempt_at→now without resetting
  attempt_count, so the existing attempt history is preserved.
- After the redrive the worker successfully delivers the event (the receiver
  switches from 500→200 after the first 3 requests, simulating "the
  target endpoint was fixed").
- The final delivery shows the complete attempt history across both phases.

The configurable_receiver ``--fail-count 3`` flag makes requests 1-3 return
HTTP 500 (driving the delivery to DEAD_LETTERED with MAX_DELIVERY_ATTEMPTS=3)
and request 4 onward return HTTP 200 (succeeding after the redrive).

Run from the project root::

    python -m demo.scenario_2_redrive
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
    do_redrive,
    failed,
    get_delivery,
    get_event,
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
    wait_for_status,
)

_LABEL = "Scenario 2 – Redrive after Dead-letter"
_RECEIVER_PORT = 8892


def run() -> None:
    section(_LABEL)
    info(f"API: {API_BASE_URL}")
    info(
        f"Worker env: MAX_DELIVERY_ATTEMPTS={WORKER_DEMO_ENV['MAX_DELIVERY_ATTEMPTS']} "
        f"RETRY_BASE_SECONDS={WORKER_DEMO_ENV['RETRY_BASE_SECONDS']}s"
    )

    # ── Setup ────────────────────────────────────────────────────────────────
    # fail_count=3: first 3 requests return 500, request 4+ returns 200.
    # With MAX_DELIVERY_ATTEMPTS=3 the delivery dead-letters on request 3.
    # After redrive the 4th request succeeds automatically.
    divider("Setup — receiver fails first 3 requests, then succeeds")
    receiver = start_receiver(_RECEIVER_PORT, status=200, fail_count=3)
    step(f"receiver pid={receiver.pid} → HTTP 500×3 then HTTP 200 on port {_RECEIVER_PORT}")

    _project_id, api_key = setup_project("demo-s2")
    endpoint_url = f"http://localhost:{_RECEIVER_PORT}/"
    create_endpoint(api_key, endpoint_url, ["order.created"])

    # ── Phase 1: drive to dead-letter ────────────────────────────────────────
    divider("Phase 1 — drive delivery to DEAD_LETTERED")
    result = post_event(api_key, "order.created", {"order_id": "ord-002", "amount": 50})
    event_id: str = str(result["event_id"])

    event_detail = get_event(api_key, event_id)
    if not event_detail["deliveries"]:
        failed(_LABEL, "no delivery was queued for the event")
        return
    delivery_id: str = str(event_detail["deliveries"][0]["id"])
    step(f"delivery_id {delivery_id[:8]}…")

    worker = start_worker()
    step(f"worker pid={worker.pid}")

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
                info(f"attempt #{a['attempt_number']}  HTTP {http}  {ms}ms")
            seen_count = d["attempt_count"]
        if d["status"] == "dead_lettered":
            step(f"delivery DEAD_LETTERED after {seen_count} failed attempts ✓")
            break
        time.sleep(0.5)
    else:
        failed(_LABEL, "delivery did not reach dead_lettered within timeout")
        return

    # Keep the worker running for the redrive phase — it will pick the delivery
    # up again once we reset it to pending.

    # ── Phase 2: redrive ─────────────────────────────────────────────────────
    divider("Phase 2 — redrive (receiver now returns 200 for request #4)")
    redriven = do_redrive(api_key, delivery_id)
    step(f"POST /deliveries/{delivery_id[:8]}…/redrive → status={redriven['status']}")
    assert redriven["status"] == "pending", (
        f"expected pending after redrive, got {redriven['status']}"
    )
    step("waiting for worker to pick up and deliver…")

    wait_for_status(api_key, delivery_id, "succeeded", timeout=30)
    step("delivery SUCCEEDED ✓")

    # ── Final state ──────────────────────────────────────────────────────────
    divider("Final delivery state (all attempts preserved)")
    d = get_delivery(api_key, delivery_id)
    attempts = list_attempts(api_key, delivery_id)
    print_delivery_summary(d, attempts)

    # ── Verify acceptance criteria ───────────────────────────────────────────
    assert d["status"] == "succeeded", f"expected succeeded, got {d['status']}"
    assert d["attempt_count"] == max_attempts + 1, (
        f"expected {max_attempts + 1} total attempts, got {d['attempt_count']}"
    )
    assert len(attempts) == max_attempts + 1
    failed_attempts = [a for a in attempts if a["response_status"] != 200]
    assert len(failed_attempts) == max_attempts
    success_attempts = [a for a in attempts if a["response_status"] == 200]
    assert len(success_attempts) == 1

    # ── Teardown ─────────────────────────────────────────────────────────────
    stop_proc(worker, "worker")
    stop_proc(receiver, "receiver")

    passed(_LABEL)


if __name__ == "__main__":
    run()
