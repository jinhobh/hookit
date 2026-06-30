# Demo Scenarios

Reproducible scripts that demonstrate the platform's reliability guarantees
against a local stack. Each scenario provisions its own project, API key, and
webhook endpoint via the public API, runs a local receiver and worker subprocess,
and prints the observed delivery and attempt states.

---

## Prerequisites

```bash
# 1. Start Postgres
docker compose up -d postgres

# 2. Apply migrations
alembic upgrade head

# 3. Start the API server (keep this running in a separate terminal)
uvicorn app.main:app --reload
```

---

## Run all scenarios

```bash
bash demo/run_all.sh
```

Or run individual scenarios:

```bash
python -m demo.scenario_1_failure_backoff_deadletter
python -m demo.scenario_2_redrive
python -m demo.scenario_3_idempotency
python -m demo.scenario_4_crash_recovery
```

---

## Scenarios

### Scenario 1 – Failure → Exponential Backoff → Dead-letter

**What it shows:** A webhook endpoint that always returns HTTP 500 drives a
delivery through the full retry cycle. Each failed attempt produces a
`delivery_attempt` row. The `next_attempt_at` timestamp advances by exponential
backoff (base 3 s, doubling each attempt). After `MAX_DELIVERY_ATTEMPTS` (3 in
demo mode) the delivery transitions to `DEAD_LETTERED`.

**Key output:**

```
attempt #1  HTTP 500  12ms
attempt #2  HTTP 500  11ms  (next retry ≥ 2026-01-01T00:00:03Z)
attempt #3  HTTP 500  10ms
delivery reached DEAD_LETTERED after 3/3 attempts ✓

delivery_id   : <uuid>
status        : dead_lettered
attempt_count : 3

#     http    ms      error
----- ------- ------- ----------------------------------------
1     500     12
2     500     11
3     500     10
```

---

### Scenario 2 – Redrive after Dead-letter

**What it shows:** A `DEAD_LETTERED` delivery can be re-queued via
`POST /deliveries/{id}/redrive`. The redrive sets `status=pending` and
`next_attempt_at=now` without resetting `attempt_count`, so the complete attempt
history is preserved. The receiver is configured with `--fail-count 3` so
requests 1–3 return 500 (driving the dead-letter) and request 4 returns 200
(succeeding after the redrive) — all without restarting the receiver.

**Key output:**

```
Phase 1 — attempt #1 HTTP 500 ... attempt #3 HTTP 500 → DEAD_LETTERED
Phase 2 — POST /redrive → pending → worker picks up → attempt #4 HTTP 200 → SUCCEEDED

delivery_id   : <uuid>
status        : succeeded
attempt_count : 4   ← pre-redrive attempts preserved

#     http    ms
----- ------- -------
1     500     12
2     500     11
3     500     10
4     200     14      ← successful attempt after redrive
```

---

### Scenario 3 – Idempotent Event Publishing

**What it shows:** Replaying `POST /events` with the same `Idempotency-Key`
header returns the identical `event_id` and `queued_deliveries` — no duplicate
event or delivery rows are created. A different key on the same payload produces
a distinct event. A payload mismatch on a known key returns HTTP 409.

No worker is required — idempotency is enforced in the ingestion layer.

**Key output:**

```
First POST  → event_id=abc12345…  queued_deliveries=1
Replay POST → event_id=abc12345…  queued_deliveries=1  ← same!
event has 1 delivery/deliveries (not 2) ✓
Different key → event_id=def67890… (distinct) ✓
Payload mismatch → HTTP 409 ✓
```

---

### Scenario 4 – At-least-once Delivery under Worker Crash

**What it shows:** The delivery worker uses a single PostgreSQL transaction that
atomically encompasses claiming the delivery (`IN_FLIGHT`) and recording the
result. If the worker is `SIGKILL`-ed while the HTTP request is in flight, the
database transaction rolls back: the delivery safely returns to `PENDING` with no
stuck or permanently lost rows. A restarted worker picks it up and drives it to
`SUCCEEDED`.

The receiver is configured with a 5-second artificial latency so the worker is
reliably blocked mid-request when killed.

**Why the delivery goes back to PENDING (not stuck IN_FLIGHT):** PostgreSQL
detects the dropped connection and rolls back any uncommitted changes. The
`leased_until` column provides a belt-and-suspenders safety net for slower
failure modes (network partition, long `idle_in_transaction_session_timeout`):
`_recover_expired_leases()` resets stale `IN_FLIGHT` rows on each worker poll
cycle.

**Key output:**

```
worker pid=... killed (SIGKILL)
delivery state after crash: status=pending  attempt_count=0
No permanently stuck row ✓ (transaction rolled back by PostgreSQL)
worker pid=... restarted → delivery SUCCEEDED ✓

delivery_id   : <uuid>
status        : succeeded
attempt_count : 1

#     http    ms
----- ------- -------
1     200     5012    ← delivered on recovery
```

---

## Configurable Receiver

`tools/configurable_receiver.py` is used by the demo scenarios. It can also be
started manually for ad-hoc testing:

```bash
# Always return HTTP 200 (default):
python tools/configurable_receiver.py --port 8889

# Always return HTTP 500:
python tools/configurable_receiver.py --port 8889 --status 500

# Return 500 for first 3 requests, then 200 (redrive demo pattern):
python tools/configurable_receiver.py --port 8889 --fail-count 3

# Add artificial latency:
python tools/configurable_receiver.py --port 8889 --latency 5

# Verify HMAC-SHA256 signatures:
python tools/configurable_receiver.py --port 8889 --verify --secret <secret>
```

---

## Demo Environment Overrides

The scenario scripts override worker settings to keep demo runtimes short:

| Variable               | Demo value | Production default |
|------------------------|------------|--------------------|
| `MAX_DELIVERY_ATTEMPTS`| 3          | 6                  |
| `RETRY_BASE_SECONDS`   | 3          | 10                 |
| `RETRY_CAP_SECONDS`    | 60         | 3600               |
| `DELIVERY_TIMEOUT_SECONDS` | 10     | 10                 |

Set `API_BASE_URL` to override the default `http://localhost:8000`.
