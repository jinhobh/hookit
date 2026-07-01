# Architecture — Reliable Webhook Delivery Platform

How the system is built. Pairs with [`PROJECT_SPEC.md`](PROJECT_SPEC.md) (what)
and [`ROADMAP.md`](ROADMAP.md) (build order).

> **Guiding principle:** start simple and correct. The MVP uses **Postgres-backed
> persistence and a simple worker** for everything, including the delivery job
> queue. Only after that is solid and measured do we consider Redis, Kafka, SQS,
> or other queues — and only behind an explicit issue.

---

## 1. High-level architecture

Two processes share one PostgreSQL database:

- **API (FastAPI)** — synchronous request handling: auth, idempotency,
  validation, persistence, and fan-out of events into delivery rows. It does
  *not* make outbound webhook calls.
- **Worker** — a loop that claims due deliveries, performs the signed HTTP POST,
  records the attempt, and either completes, schedules a retry, or dead-letters.

```
clients ──HTTP──▶ FastAPI ──writes──▶  PostgreSQL  ◀──reads/claims── Worker ──HTTP──▶ receivers
                                   (events, deliveries,
                                    attempts, idempotency)
```

Decoupling ingestion from delivery is the core reliability move: a slow receiver
can never slow down ingestion.

## 2. Components

- `app/main.py` — FastAPI app factory and router wiring.
- `app/core/config.py` — Pydantic Settings (single source of configuration).
- `app/api/` *(future)* — routers per resource (projects, keys, endpoints,
  events, deliveries).
- `app/models/` *(future)* — SQLAlchemy 2.x ORM models.
- `app/db/` *(future)* — engine/session factory; Alembic env.
- `app/services/` *(future)* — domain logic: idempotency, fan-out, signing,
  retry policy.
- `app/worker/` *(future)* — the delivery loop and claim/lease logic.
- `migrations/` *(future, Alembic)* — versioned schema.

Pure logic (signing, backoff computation, idempotency comparison) lives in
services and is unit-tested without I/O.

## 3. Data flow

**Ingestion (`POST /events`)**
1. Authenticate the bearer API key (hash lookup, project scoping).
2. Look up `(project_id, idempotency_key)`; if present, return the stored result.
3. Validate payload; insert `Event`.
4. Select endpoints subscribed to the event type; insert one `Delivery` per
   endpoint with `status=PENDING`, `next_attempt_at=now()`.
5. Persist the idempotency record (event id + queued count + request hash) in the
   same transaction.
6. Return `{ event_id, queued_deliveries }`.

**Delivery (worker loop)**
1. Atomically **claim** a batch of due deliveries
   (`status=PENDING AND next_attempt_at <= now()`), marking them `IN_FLIGHT` with
   a lease.
2. For each: build canonical payload, compute HMAC signature, POST with timeout.
3. Write a `DeliveryAttempt` (status code, truncated body, error, duration).
4. On 2xx → `SUCCEEDED`. On failure with attempts remaining → compute backoff,
   set `next_attempt_at`, return to `PENDING`. On exhaustion → `DEAD_LETTERED`.

## 4. Future database schema (indicative)

```
projects(id pk, name, created_at)

api_keys(id pk, project_id fk, key_hash unique, prefix, name,
         created_at, last_used_at, revoked_at)

endpoints(id pk, project_id fk, url, event_types text[], secret_enc,
          status, created_at, updated_at)

events(id pk, project_id fk, type, payload jsonb, idempotency_key,
       created_at,
       unique(project_id, idempotency_key))

deliveries(id pk, event_id fk, endpoint_id fk, status,
           attempt_count, next_attempt_at, leased_until,
           created_at, updated_at,
           index(status, next_attempt_at))

delivery_attempts(id pk, delivery_id fk, attempt_number,
                  request_signature, response_status, response_body_excerpt,
                  error, duration_ms, created_at)
```

All changes are introduced via Alembic migrations; no `create_all` in production
paths.

## 5. Worker design

- Single-process polling loop in the MVP (`SELECT … FOR UPDATE SKIP LOCKED` to
  claim work safely under concurrency, enabling horizontal scaling later without
  schema changes).
- **Leases**: a claimed delivery sets `leased_until`. If a worker dies, the lease
  expires and the delivery becomes eligible again — no lost work.
- Bounded batch size and per-attempt timeout to keep the loop responsive.
- Idempotent at the delivery grain: re-delivering after a crash is acceptable
  (at-least-once); receivers dedupe via the signed event id.

## 6. Retry design

- Backoff is a **pure function** of attempt number:
  `delay = min(base * 2 ** (attempt - 1), cap) + jitter`.
- Jitter (full or equal jitter) avoids thundering herds.
- Config-driven (`base`, `cap`, `max_attempts`) via `app/core/config.py`.
- Exhaustion → `DEAD_LETTERED`; redrive resets to `PENDING` while preserving
  attempt history.

## 7. Idempotency design

- Header `Idempotency-Key`, unique per project, stored in a durable table keyed
  by `(project_id, idempotency_key)`.
- The create-event + create-deliveries + store-idempotency-record steps happen in
  **one transaction**, so a replay either sees the full result or none of it.
- The stored record includes a hash of the request body; a key reused with a
  different body is rejected (409).

## 8. HMAC signing design

- Canonical signing string: `f"{timestamp}.{raw_body}"`.
- `signature = HMAC_SHA256(endpoint_secret, signing_string)`, hex-encoded.
- Sent as headers, e.g. `X-Webhook-Signature` and `X-Webhook-Timestamp`.
- Receivers recompute and compare in **constant time**; the timestamp bounds
  replay windows.
- Endpoint secrets are stored encrypted/secured, never logged.

## 9. Observability plan

- **Structured logging** with correlation ids (event id, delivery id, attempt
  number); secrets and full signatures are never logged.
- **Attempt log as a first-class audit trail** — every network attempt is a row,
  queryable via the API.
- Counters/timers (deliveries succeeded/failed/dead-lettered, attempt latency)
  are exposed in a later phase; Prometheus/OpenTelemetry is a future extension,
  not MVP.

## 10. Tradeoffs

- **Postgres-as-queue vs. a broker.** Chosen for simplicity, transactional fan-out
  (event + deliveries + idempotency in one commit), and zero extra infra.
  `FOR UPDATE SKIP LOCKED` scales to meaningful throughput. A dedicated broker is
  a deliberate *later* decision once limits are measured.
- **Polling worker vs. event-driven.** Polling is simple and robust; latency is
  bounded by poll interval. Acceptable for webhook delivery SLAs; can add
  `LISTEN/NOTIFY` later.
- **At-least-once vs. exactly-once.** Exactly-once over HTTP is impractical; we
  choose at-least-once + signing + event ids so receivers can dedupe.
- **Single worker process (MVP).** Simple to reason about; the claim model already
  supports running multiple workers when needed.

## 11. Live simulation (`POST /simulate/run`)

Powers the dashboard's "Simulate load" button. Design constraints and how they're met:

- **Reuse real code paths, not test doubles.** The batch is published through
  the same `ingest_event()` every real client uses, and delivered through the
  same `process_delivery()` the worker uses — real HMAC signing, a real HTTP
  round trip to a real (self-referential) receiver, real `DeliveryAttempt`
  rows.
- **Two-phase commit, deliberately.** `app/services/simulate.py::run_simulation`
  commits once after publishing the batch (so the out-of-process worker and the
  `/simulate/receiver` route — each resolving their own DB session — can see
  the rows under Postgres' `READ COMMITTED` default) and again after the
  fast-forward. This is a documented exception to the usual "router commits
  once" convention.
- **One delivery is fast-forwarded, not fabricated.** Real backoff (base=10s,
  cap=1h, 6 attempts) would take ~5 real minutes to reach dead-lettered. The
  "always fails" delivery in the batch instead has `process_delivery()` called
  back-to-back with no wait on `next_attempt_at` — real signing and recording,
  only the *wait* is skipped, isolated in `_fast_forward_to_dead_letter`.
- **Concurrency-safe against the real worker.** The target delivery is loaded
  with a blocking `SELECT ... FOR UPDATE` (not `SKIP LOCKED`) bounded by a 5s
  `lock_timeout`, so the live worker process racing to claim the same
  just-committed row can't cause a lost update; `find_or_create_demo_endpoint`
  guards its check-then-insert with `pg_advisory_xact_lock` since there's no
  unique constraint to catch two concurrent first-clicks creating duplicate
  demo endpoints.
- **No new trust boundary.** The receiver route only ever answers
  200/401/404/500 for its own reserved (`__simulate__`-tagged) endpoints — it
  never initiates a request — and `/simulate/run` is scoped by the caller's own
  project API key exactly like every other authenticated endpoint.
