"""Scenario 3: Idempotent event publishing.

Demonstrates:
- Replaying POST /events with the same Idempotency-Key header returns
  the same event_id and queued_deliveries count — no duplicate event or
  delivery rows are created.
- A different Idempotency-Key on an identical payload produces a distinct event.
- A payload mismatch on a known key returns HTTP 409 Conflict.

No worker is needed for this scenario — idempotency is enforced at the ingestion
layer, not the delivery layer.

Run from the project root::

    python -m demo.scenario_3_idempotency
"""

from __future__ import annotations

import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo._shared import (  # noqa: E402
    API_BASE_URL,
    create_endpoint,
    divider,
    get_event,
    info,
    passed,
    post_event,
    section,
    setup_project,
    start_receiver,
    step,
    stop_proc,
)

_LABEL = "Scenario 3 – Idempotent event publishing"
_RECEIVER_PORT = 8893


def run() -> None:
    section(_LABEL)
    info(f"API: {API_BASE_URL}")

    # ── Setup ────────────────────────────────────────────────────────────────
    divider("Setup")
    receiver = start_receiver(_RECEIVER_PORT, status=200)
    step(f"receiver pid={receiver.pid} → HTTP 200 on port {_RECEIVER_PORT}")

    _project_id, api_key = setup_project("demo-s3")
    endpoint_url = f"http://localhost:{_RECEIVER_PORT}/"
    create_endpoint(api_key, endpoint_url, ["order.created"])

    payload = {"order_id": "ord-003", "amount": 75}
    idempotency_key = "demo-idem-key-ord-003"

    # ── First publish ────────────────────────────────────────────────────────
    divider("First POST /events (new event)")
    r1 = post_event(api_key, "order.created", payload, idempotency_key=idempotency_key)
    event_id_1: str = str(r1["event_id"])
    queued_1: int = int(r1["queued_deliveries"])
    info(f"event_id={event_id_1}  queued_deliveries={queued_1}")

    # ── Replay with same key ─────────────────────────────────────────────────
    divider("Replay — same Idempotency-Key, same payload")
    r2 = post_event(api_key, "order.created", payload, idempotency_key=idempotency_key)
    event_id_2: str = str(r2["event_id"])
    queued_2: int = int(r2["queued_deliveries"])
    info(f"event_id={event_id_2}  queued_deliveries={queued_2}")

    assert event_id_2 == event_id_1, (
        f"replay returned a different event_id: {event_id_2} ≠ {event_id_1}"
    )
    assert queued_2 == queued_1, (
        f"replay returned different queued_deliveries: {queued_2} ≠ {queued_1}"
    )
    step("same event_id and queued_deliveries returned — no duplicate created ✓")

    # ── Verify single event in database ─────────────────────────────────────
    divider("Verify: GET /events/{id} shows one delivery, not two")
    event = get_event(api_key, event_id_1)
    delivery_count = len(event["deliveries"])
    info(f"event has {delivery_count} delivery/deliveries")
    assert delivery_count == 1, f"expected 1 delivery, found {delivery_count}"
    step("single delivery confirmed — no fan-out duplication ✓")

    # ── Different key → new event ─────────────────────────────────────────────
    divider("Different Idempotency-Key → distinct event")
    r3 = post_event(api_key, "order.created", payload, idempotency_key="demo-idem-key-ord-003-v2")
    event_id_3: str = str(r3["event_id"])
    info(f"event_id={event_id_3}")
    assert event_id_3 != event_id_1, "expected a fresh event_id for a new key"
    step("different key produced a distinct event ✓")

    # ── Payload mismatch → 409 ───────────────────────────────────────────────
    divider("Payload mismatch on known key → HTTP 409")
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": idempotency_key,
    }
    conflict_response = httpx.post(
        f"{API_BASE_URL}/events",
        headers=headers,
        json={"type": "order.created", "payload": {"order_id": "DIFFERENT", "amount": 0}},
        timeout=10,
    )
    info(f"HTTP {conflict_response.status_code}: {conflict_response.json()}")
    assert conflict_response.status_code == 409, (
        f"expected 409 Conflict, got {conflict_response.status_code}"
    )
    step("payload mismatch correctly rejected with 409 ✓")

    # ── Teardown ─────────────────────────────────────────────────────────────
    stop_proc(receiver, "receiver")

    passed(_LABEL)


if __name__ == "__main__":
    run()
