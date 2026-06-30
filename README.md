# Reliable Webhook Delivery Platform

**At-least-once delivery, exponential backoff with jitter, idempotency, dead-letter
queuing, manual redrive, HMAC-SHA256 signing, and Postgres-as-queue — no external
broker required.**

> **Status: Phases 0–9 complete.** The API, delivery worker, retries, dead-lettering,
> inspection endpoints, manual redrive, and end-to-end tests are all live.
> See [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## Live demo

> **[Live demo URL — to be added after deployment]**
>
> ![Demo GIF — to be recorded after deployment](docs/demo.gif)

---

## Architecture

Two processes share one PostgreSQL database. Ingestion and delivery are decoupled
so a slow receiver never blocks event publishing.

```mermaid
graph LR
    C1["API Client A"] -->|Bearer token + Idempotency-Key| API
    C2["API Client B"] -->|Bearer token + Idempotency-Key| API

    subgraph FastAPI["FastAPI (app/main.py)"]
        AUTH["Auth\n(hashed key lookup)"]
        IDEM["Idempotency\ncheck"]
        FAN["Fan-out to\nDelivery rows"]
        AUTH --> IDEM --> FAN
    end

    API --> AUTH

    FAN -->|"INSERT events + deliveries\n(one transaction)"| PG[("PostgreSQL\nevents · deliveries\nattempts · idempotency")]

    PG -->|"SELECT FOR UPDATE\nSKIP LOCKED"| Worker

    subgraph Worker["Worker (app/worker)"]
        CLAIM["Claim batch"]
        SIGN["HMAC-sign payload"]
        POST["POST with timeout"]
        RECORD["Record attempt"]
        RETRY["Retry / backoff\nor dead-letter"]
        CLAIM --> SIGN --> POST --> RECORD --> RETRY
    end

    Worker -->|"signed POST"| R1["Receiver A"]
    Worker -->|"signed POST"| R2["Receiver B"]
    Worker -->|"signed POST"| R3["Receiver N"]
```

### Delivery lifecycle

```mermaid
sequenceDiagram
    participant C as API Client
    participant API as FastAPI
    participant DB as PostgreSQL
    participant W as Worker
    participant R as Receiver

    C->>API: POST /events<br/>(Bearer, Idempotency-Key, payload)
    API->>DB: Check idempotency record
    alt Duplicate key, same body
        DB-->>API: Stored result
        API-->>C: 200 (cached)
    else New request
        API->>DB: INSERT event + deliveries + idempotency (1 txn)
        API-->>C: 201 {event_id, queued_deliveries}
    end

    loop Worker poll
        W->>DB: SELECT … FOR UPDATE SKIP LOCKED (claim batch)
        DB-->>W: Delivery rows → IN_FLIGHT
        W->>W: Build canonical payload, compute HMAC
        W->>R: POST (X-Webhook-Signature, X-Webhook-Timestamp)
        alt 2xx
            R-->>W: 200 OK
            W->>DB: status = SUCCEEDED, write DeliveryAttempt
        else Non-2xx / timeout, attempts remaining
            R-->>W: 5xx / timeout
            W->>DB: write DeliveryAttempt, schedule retry<br/>next_attempt_at = now() + backoff+jitter
            Note over W,DB: status → PENDING (retriable)
        else Attempts exhausted
            W->>DB: status = DEAD_LETTERED, write DeliveryAttempt
        end
    end

    C->>API: POST /deliveries/{id}/redrive
    API->>DB: status → PENDING, preserve attempt history
    Note over DB,W: Delivery re-enters the worker loop
```

Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Design decisions & tradeoffs

### Postgres-as-queue vs. a broker

PostgreSQL handles both persistence *and* the delivery job queue. Fan-out (event
+ deliveries + idempotency record) lands in **one atomic transaction**, so there
is no window between "event stored" and "delivery enqueued." `SELECT … FOR UPDATE
SKIP LOCKED` lets multiple workers claim non-overlapping batches safely without
extra coordination. A dedicated broker (Redis, SQS, Kafka) is a deliberate
*later* decision, made only once Postgres throughput is measured and found
insufficient.

### `FOR UPDATE SKIP LOCKED`

The claim query locks exactly the rows it takes and skips any locked by a
concurrent worker — no deadlocks, no thundering-herd on the queue table. Each
worker processes its batch independently; horizontal scaling is a matter of
running more worker processes.

### Exponential backoff with jitter

Retry delay follows `min(base × 2^(attempt−1), cap) + random_jitter`. The jitter
(full or equal) spreads retries over time so a mass failure at one receiver does
not synchronise retries into a new spike. Defaults (`base=10s`, `cap=1h`,
`max_attempts=6`) are config-driven and tunable without a code change.

### At-least-once vs. exactly-once

Exactly-once delivery over HTTP is impractical: acknowledgements can be lost even
after a successful POST. This service chooses at-least-once + HMAC signing +
stable event IDs so that receivers can deduplicate on their side when they need
to. Workers re-deliver after a crash; claim leases (`leased_until`) ensure stuck
deliveries become eligible again rather than being silently dropped.

### SSRF handling of untrusted target URLs

Webhook target URLs are supplied by authenticated clients but treated as
untrusted. The architecture document flags SSRF risk; a future hardening issue
will enforce an allowlist or block private IP ranges at the egress layer.

---

## Benchmark numbers

> **[Benchmark results — to be filled in after the benchmark run]**
>
> See [`tests/benchmark/`](tests/benchmark/) for the harness.
> Preliminary targets: ≥ 500 events/s ingest throughput, p99 delivery latency < 2 s
> under a single worker at moderate queue depth.

---

## Quality bar

All four checks run identically in CI (`.github/workflows/ci.yml`) and must pass
on every PR:

```bash
ruff format --check .   # formatting
ruff check .            # lint
mypy app tests          # strict static types
pytest                  # real-Postgres transactional tests (no mocks)
```

`mypy` runs in strict mode; the test suite hits a live PostgreSQL instance spun
up by the CI service — no database mocking. A PR that breaks any of these four
checks cannot merge.

---

## Autonomous agent loop

Development is driven by a self-advancing GitHub Actions pipeline built on
[`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action):
a **Planner** agent keeps a backlog of small, well-specified `agent:ready` issues;
a **Builder** picks the oldest issue, implements the smallest complete solution,
and opens a PR; a **Reviewer** approves or requests changes; and an **auto-merge**
workflow squash-merges any PR that is CI-green and Reviewer-approved, which
re-triggers the Builder for the next issue — no human merge required. Full
details: [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md). Agent rules live in
[`CLAUDE.md`](CLAUDE.md) and [`agents/`](agents/).

---

## Run locally

Requires Python 3.12 and Docker.

```bash
# Clone, then create a virtual environment
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configure
cp .env.example .env

# Start Postgres
docker compose up -d postgres

# Run database migrations
alembic upgrade head

# Run the API
uvicorn app.main:app --reload
# → http://localhost:8000/health  →  {"status": "ok"}

# In a separate terminal, run the delivery worker
python -m app.worker
```

## Run tests & quality checks

```bash
ruff format --check .
ruff check .
mypy app tests
pytest
```

## API quick-start

```http
POST /events
Authorization: Bearer <api_key>
Idempotency-Key: <unique-key>
Content-Type: application/json

{ "type": "user.created", "payload": { "user_id": "abc123", "email": "test@example.com" } }
```

Response:

```json
{ "event_id": "evt_...", "queued_deliveries": 2 }
```

The system authenticates the key, enforces idempotency, stores the event, fans
out to subscribed endpoints, and returns. The worker delivers asynchronously,
records every attempt, and retries failures on the backoff schedule. Dead-lettered
deliveries can be redriven via `POST /deliveries/{id}/redrive`.

---

## Human owner responsibilities

The human owner ([@jinhobh](https://github.com/jinhobh)) only needs to:

1. Keep `CLAUDE_CODE_OAUTH_TOKEN` (Claude Pro/Max) or `ANTHROPIC_API_KEY`
   configured in the repo secrets.
2. Keep `AGENT_GH_TOKEN` (PAT) valid so agent actions re-trigger the next
   workflow.
3. Intervene when an agent is stuck or to steer the roadmap.

To restore a human merge gate, delete `.github/workflows/auto-merge.yml`.

---

## License

MIT.
