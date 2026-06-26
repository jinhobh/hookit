# Project Specification — Reliable Webhook Delivery Platform

This document defines **what** the product is. For **how** it is built, see
[`ARCHITECTURE.md`](ARCHITECTURE.md); for the build order, see
[`ROADMAP.md`](ROADMAP.md).

---

## 1. Problem statement

Applications need to notify external systems when events happen ("user.created",
"order.paid"). Naively POSTing to a URL is unreliable: receivers are down or
slow, networks drop, requests get duplicated, and there is no audit trail or
recovery path. Teams repeatedly rebuild the same fragile delivery logic.

The Reliable Webhook Delivery Platform is a backend service that accepts events
from authenticated clients and **guarantees durable, at-least-once, signed
delivery** to registered endpoints, with retries, dead-lettering, redrive, and
full inspection APIs.

## 2. User stories

As an **API client / integrator**, I want to:

- Create a **project** to scope my resources.
- Create **API keys** for a project so I can authenticate requests.
- Register **webhook endpoints** (URL + event-type subscriptions + signing
  secret).
- **Publish events** with an idempotency key so retries on my side don't create
  duplicates.
- Trust that delivery is **attempted reliably** and **retried** on failure.
- **Verify authenticity** of received webhooks via an HMAC signature.
- **Inspect** events, deliveries, and individual attempts to debug integrations.
- **Redrive** deliveries that permanently failed, after I fix my endpoint.

As the **operator/owner**, I want clear failure states (dead-letter), audit logs
of every attempt, and safe, migration-driven schema evolution.

## 3. Core entities

| Entity | Purpose |
| --- | --- |
| **Project** | Top-level tenant boundary owning keys, endpoints, and events. |
| **ApiKey** | Project-scoped credential (stored hashed) used as a bearer token. |
| **Endpoint** | A registered webhook target: URL, subscribed event types, signing secret, status. |
| **Event** | An ingested event: type + JSON payload + idempotency key + project. |
| **Delivery** | A unit of work: deliver one Event to one Endpoint; tracks status and next-attempt time. |
| **DeliveryAttempt** | A single HTTP attempt for a Delivery: request meta, response status/body, error, duration, timestamp. |

A published Event fans out into **one Delivery per matching Endpoint**.

## 4. API surface (eventually planned)

All non-public endpoints require `Authorization: Bearer <api_key>`. Mutating
event ingestion supports `Idempotency-Key`.

**System**
- `GET /health` → `{"status": "ok"}` *(implemented in skeleton)*

**Projects & keys** *(bootstrap/admin)*
- `POST /projects` — create a project
- `POST /projects/{id}/api-keys` — mint a project-scoped API key (returned once)

**Endpoints**
- `POST /endpoints` — register an endpoint (url, event types, secret)
- `GET /endpoints` — list endpoints for the authenticated project
- `PATCH /endpoints/{id}` — update (pause/activate, change subscriptions)
- `DELETE /endpoints/{id}` — remove an endpoint

**Events**
- `POST /events` — publish an event (auth + idempotency; returns event id +
  queued delivery count)
- `GET /events` — list events
- `GET /events/{id}` — event detail with its deliveries

**Deliveries & attempts (inspection + recovery)**
- `GET /deliveries` — list/filter deliveries (by status, endpoint, event)
- `GET /deliveries/{id}` — delivery detail
- `GET /deliveries/{id}/attempts` — attempt log
- `POST /deliveries/{id}/redrive` — re-queue a dead-lettered delivery

## 5. Reliability requirements

- **At-least-once delivery.** A delivery is attempted until it succeeds or is
  dead-lettered. Receivers must therefore tolerate duplicates (we sign and may
  include an event id to help dedupe).
- **Durable queue.** Pending deliveries survive process restarts (Postgres-backed,
  not in-memory).
- **No lost work.** Crash mid-attempt must not silently drop a delivery; claims
  are visible and time-bounded so stuck deliveries become eligible again.
- **Backpressure-friendly.** A slow/broken endpoint must not block delivery to
  other endpoints.

## 6. Security requirements

- API keys are random, high-entropy, and stored **hashed**; the plaintext is
  shown to the client exactly once at creation.
- Outbound requests are signed with **HMAC-SHA256** over a canonical string and a
  timestamp; signatures use the endpoint's secret. Verification examples are
  documented for receivers.
- Secrets are never logged. Logs may include identifiers and truncated,
  non-sensitive response bodies only.
- Inbound payloads are validated/bounded (size limits, schema) via Pydantic.
- Webhook target URLs are treated as untrusted (SSRF awareness).

## 7. Idempotency requirements

- `POST /events` accepts an `Idempotency-Key` header, **unique per project**.
- The first request with a given key creates the event and its deliveries; later
  requests with the **same key** return the **same** event id and queued count
  without creating duplicates.
- Idempotency records are stored durably and keyed by `(project_id,
  idempotency_key)`.
- A reused key with a **different** request body is rejected with a clear error.

## 8. Delivery lifecycle

```
PENDING ──claimed──▶ IN_FLIGHT ──2xx──▶ SUCCEEDED
   ▲                     │
   │                     ├── non-2xx / error, attempts remaining ──▶ schedule retry ──▶ PENDING (next_attempt_at)
   │                     │
   └─────────────────────┴── attempts exhausted ──▶ DEAD_LETTERED ──redrive──▶ PENDING
```

States: `PENDING`, `IN_FLIGHT`, `SUCCEEDED`, `FAILED` (retriable, awaiting next
attempt), `DEAD_LETTERED`. Every transition that hits the network writes a
`DeliveryAttempt`.

## 9. Retry lifecycle

- Failures (connection error, timeout, non-2xx) increment the attempt count.
- Next attempt time uses **exponential backoff with jitter**:
  `delay = min(base * 2 ** (attempt - 1), cap) + random_jitter`.
- Defaults (tunable via config): `base = 10s`, `cap = 1h`, `max_attempts = 6`.
- When attempts reach `max_attempts`, the delivery is `DEAD_LETTERED`.
- **Redrive** resets a dead-lettered delivery to `PENDING` for immediate
  eligibility (attempt history is preserved).

## 10. Non-goals (MVP)

Out of scope for the MVP (possible future extensions): frontend dashboard,
multi-tenant billing, Kubernetes, Kafka, OAuth, complex user management, email
notifications, analytics dashboards, cloud deployment, and external message
brokers (Redis/SQS). The MVP stays focused on backend fundamentals on Postgres.

## 11. MVP acceptance criteria

The MVP is "done" when:

1. A client can create a project and an API key.
2. A client can register an endpoint subscribed to one or more event types.
3. `POST /events` authenticates, enforces idempotency, stores the event, and
   creates one delivery per matching endpoint, returning the event id and queued
   count.
4. A worker delivers pending deliveries asynchronously, signing each request with
   HMAC-SHA256.
5. Every attempt is recorded with status, response metadata, error, and duration.
6. Failed deliveries retry on the backoff schedule and are dead-lettered after
   the max attempts.
7. A dead-lettered delivery can be redriven via the API.
8. Events, deliveries, and attempts are inspectable via the API.
9. All schema changes are Alembic migrations; `ruff`, `mypy`, and `pytest` pass
   in CI; core reliability logic is covered by tests.
