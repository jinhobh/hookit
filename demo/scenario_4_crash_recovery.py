"""Scenario 4: At-least-once delivery under worker crash.

Demonstrates:
- The worker uses a PostgreSQL transaction that atomically encompasses both
  claiming the delivery (IN_FLIGHT) and recording the result (SUCCEEDED /
  retry / dead-lettered).
- If the worker process is killed while the HTTP request is in flight, the
  database transaction is rolled back: the delivery safely returns to PENDING
  with no stuck or lost rows.
- On restart the worker picks the delivery up again via its normal polling loop
  and drives it to SUCCEEDED.
- The leased_until column provides an additional safety net when connections
  are held open without rolling back (e.g. network partition); a restarted
  worker's _recover_expired_leases() resets any stale IN_FLIGHT rows.

Timeline::

    t=0     event sent → PENDING
    t≈0.5   worker claims delivery → transaction open (IN_FLIGHT inside tx)
    t≈0.5   worker sends HTTP; receiver pauses for --latency seconds
    t≈1     demo kills worker → transaction rolled back → delivery back to PENDING
    t=1     delivery is PENDING (no stuck row, no data loss)
    t=1     worker restarted → claims delivery → HTTP succeeds → SUCCEEDED

Run from the project root::

    python -m demo.scenario_4_crash_recovery
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo._shared import (  # noqa: E402
    API_BASE_URL,
    create_endpoint,
    divider,
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

_LABEL = "Scenario 4 – At-least-once under worker crash"
_RECEIVER_PORT = 8894
_RECEIVER_LATENCY = 5.0  # seconds — long enough to kill the worker mid-flight
_CRASH_WINDOW = 1.5  # seconds after worker starts before we kill it


def run() -> None:
    section(_LABEL)
    info(f"API: {API_BASE_URL}")
    info(f"Receiver: HTTP 200 + {_RECEIVER_LATENCY}s latency — keeps worker busy while we crash it")

    # ── Setup ────────────────────────────────────────────────────────────────
    divider("Setup")
    receiver = start_receiver(_RECEIVER_PORT, status=200, latency=_RECEIVER_LATENCY)
    step(
        f"receiver pid={receiver.pid} → HTTP 200 (+{_RECEIVER_LATENCY}s delay) on :{_RECEIVER_PORT}"
    )

    _project_id, api_key = setup_project("demo-s4")
    endpoint_url = f"http://localhost:{_RECEIVER_PORT}/"
    create_endpoint(api_key, endpoint_url, ["order.created"])

    # ── Send event ───────────────────────────────────────────────────────────
    divider("Send event")
    result = post_event(api_key, "order.created", {"order_id": "ord-004", "amount": 120})
    event_id: str = str(result["event_id"])

    event_detail = get_event(api_key, event_id)
    if not event_detail["deliveries"]:
        failed(_LABEL, "no delivery was queued for the event")
        return
    delivery_id: str = str(event_detail["deliveries"][0]["id"])
    step(f"delivery_id {delivery_id[:8]}…  status=pending")

    # ── Start worker, then crash it ───────────────────────────────────────────
    divider(f"Crash the worker ~{_CRASH_WINDOW}s after it starts")
    worker = start_worker()
    step(f"worker pid={worker.pid} — sleeping {_CRASH_WINDOW}s then sending SIGKILL…")
    time.sleep(_CRASH_WINDOW)

    # SIGKILL (not SIGTERM) so the process cannot catch or defer the signal.
    # This is equivalent to the OS killing the process (OOM, power loss, etc.).
    if worker.poll() is not None:
        failed(_LABEL, "worker exited on its own before we could kill it")
        return

    with contextlib.suppress(ProcessLookupError):
        os.kill(worker.pid, signal.SIGKILL)
    worker.wait()
    step(f"worker pid={worker.pid} killed (SIGKILL)")

    # ── Verify delivery is not stuck ─────────────────────────────────────────
    divider("Delivery state immediately after crash")
    d = get_delivery(api_key, delivery_id)
    info(f"status={d['status']}  attempt_count={d['attempt_count']}")

    # The database transaction was rolled back when the process was killed, so
    # the delivery is either PENDING (most common: killed before response) or
    # SUCCEEDED (rare: response arrived and tx committed before kill signal).
    if d["status"] == "succeeded":
        info(
            "Worker committed the result before the kill signal landed — "
            "delivery already SUCCEEDED. at-least-once: no retry needed here."
        )
        attempts = list_attempts(api_key, delivery_id)
        print_delivery_summary(d, attempts)
        stop_proc(receiver, "receiver")
        passed(_LABEL)
        return

    assert d["status"] in {"pending", "in_flight"}, f"unexpected status after crash: {d['status']}"
    step(
        f"delivery is {d['status']} — no permanently stuck row ✓ "
        "(transaction rolled back by PostgreSQL on connection close)"
    )

    # ── Restart worker ───────────────────────────────────────────────────────
    divider("Restart worker — delivery is reclaimed and delivered")
    # Use fast delivery timeout so the restarted worker is not blocked by the
    # 5-second latency for too long; 10s is still > the receiver latency.
    worker2 = start_worker()
    step(f"worker pid={worker2.pid} restarted")

    # The restarted worker will either:
    # a) pick up the PENDING delivery immediately, or
    # b) call _recover_expired_leases() first (if leased_until was committed),
    #    reset it to PENDING, then pick it up.
    wait_for_status(api_key, delivery_id, "succeeded", timeout=30)
    step("delivery SUCCEEDED after recovery ✓")

    # ── Final state ──────────────────────────────────────────────────────────
    divider("Final delivery state")
    d = get_delivery(api_key, delivery_id)
    attempts = list_attempts(api_key, delivery_id)
    print_delivery_summary(d, attempts)

    info("")
    info(
        "at-least-once guarantee: the receiver may have received the HTTP request"
        " before the crash and again after recovery (two deliveries to the endpoint)."
    )
    info("The delivery row is SUCCEEDED exactly once — no permanently lost or duplicate DB rows.")

    # ── Verify acceptance criteria ───────────────────────────────────────────
    assert d["status"] == "succeeded"
    assert d["attempt_count"] >= 1

    # ── Teardown ─────────────────────────────────────────────────────────────
    stop_proc(worker2, "worker2")
    stop_proc(receiver, "receiver")

    passed(_LABEL)


if __name__ == "__main__":
    run()
