# Reliable Webhook Delivery Platform

A production-style backend service that ingests events from authenticated API
clients and delivers them **reliably and asynchronously** to registered webhook
endpoints — with idempotency, HMAC signing, retries with exponential backoff,
dead-lettering, and manual redrive.

> **Status: project skeleton only.** The repository currently contains a minimal
> FastAPI app (a `/health` endpoint), the full engineering documentation, and an
> autonomous agent workflow that builds the product incrementally through GitHub
> issues and pull requests. See [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## 1. What this is

A backend that solves a deceptively hard problem: **delivering webhooks
reliably.** Networks fail, receivers time out, duplicates happen, and clients
expect ordering, signatures, and a way to recover from failures. This service
provides a clean API to publish events and a robust delivery subsystem that
keeps trying — correctly — until it succeeds or is safely dead-lettered.

## 2. Why it exists

Most "webhook" tutorials stop at "POST to a URL." Real systems need durability,
idempotency, observability, and failure recovery. This project exists to
demonstrate **backend reliability engineering** end-to-end, and to serve as a
serious portfolio piece showing production-style judgment rather than a CRUD toy.

## 3. Backend concepts demonstrated

- **Asynchronous job processing** — a worker delivers events out-of-band from
  ingestion.
- **Idempotency** — `Idempotency-Key` ensures duplicate publishes don't duplicate
  events.
- **Reliability** — retries with exponential backoff + jitter, dead-letter state,
  and manual redrive.
- **Database modeling** — projects, API keys, endpoints, events, deliveries, and
  attempt logs in PostgreSQL via SQLAlchemy + Alembic migrations.
- **Security** — hashed API keys, HMAC-SHA256 request signing, constant-time
  verification, SSRF-aware egress.
- **Observability** — structured delivery/attempt logs queryable through APIs.
- **Testing & type safety** — pytest, mypy (strict), ruff, enforced in CI.

## 4. Planned architecture

```
                 ┌─────────────────────────────────────────────┐
   API client    │                FastAPI app                  │
  ───Bearer───▶  │  auth → idempotency → store event →         │
   POST /events  │  fan-out to matching endpoints (deliveries) │
                 └───────────────┬─────────────────────────────┘
                                 │ enqueue (Postgres-backed jobs)
                                 ▼
                 ┌─────────────────────────────────────────────┐
                 │                  Worker                      │
                 │  claim due deliveries → POST signed payload  │
                 │  → record attempt → retry/backoff or         │
                 │  dead-letter                                 │
                 └───────────────┬─────────────────────────────┘
                                 ▼
                          Registered webhook URLs
```

Persistence and the delivery queue both start on **PostgreSQL**; an external
broker (Redis/SQS/Kafka) is explicitly out of scope for the MVP. Full design:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### Eventual target request

```http
POST /events
Authorization: Bearer <api_key>
Idempotency-Key: <key>

{ "type": "user.created", "payload": { "user_id": "abc123", "email": "test@example.com" } }
```

The system authenticates the key, checks idempotency, stores the event, finds
subscribed endpoints, creates delivery records, and returns an event ID plus a
queued-delivery count. A worker delivers the events, records every attempt, and
retries failures per the backoff schedule.

## 5. Run locally

Requires Python 3.12 and (for the database) Docker.

```bash
# Clone, then create a virtual environment
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configure
cp .env.example .env

# Start Postgres (the app currently only needs it for later phases)
docker compose up -d postgres

# Run the API
uvicorn app.main:app --reload
# → http://localhost:8000/health  →  {"status": "ok"}
```

## 6. Run tests & quality checks

```bash
ruff format --check .
ruff check .
mypy app tests
pytest
```

These four commands are the quality gate and are run identically in CI
(`.github/workflows/ci.yml`).

## 7. How the agent workflow works

Development is driven by an autonomous, issue-queue-based agent system built on
GitHub Actions and `anthropics/claude-code-action@v1`:

- **Planner** keeps a backlog of small, well-specified `agent:ready` issues.
- **Builder** picks the oldest ready issue, implements the smallest complete
  solution on a branch, runs the checks, and opens a PR labeled
  `agent:needs-review`.
- **Reviewer** reviews each PR (review-only) and labels it `agent:approved` or
  `agent:changes-needed`.
- **CI** enforces formatting, lint, types, and tests on every PR.

Agents never merge and never push to `main`. Full details:
[`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md). Agent rules live in
[`CLAUDE.md`](CLAUDE.md) and [`agents/`](agents/).

## 8. Human owner responsibilities

This repo runs in **autonomous (auto-merge) mode**: the Planner, Builder, and
Reviewer run on their own, and the `auto-merge` workflow squash-merges any PR
that is CI-green and Reviewer-approved — which re-triggers the Builder for the
next issue. The loop self-advances with no human merge.

The human owner ([@jinhobh](https://github.com/jinhobh)) only needs to:

1. Keep the agent auth secret configured: `CLAUDE_CODE_OAUTH_TOKEN` (from a Claude
   Pro/Max subscription via `claude setup-token`) or `ANTHROPIC_API_KEY`.
2. Keep the `AGENT_GH_TOKEN` PAT secret valid (used so agent actions trigger the
   next workflow — see [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md)).
3. Intervene when an agent gets stuck (a PR stays `agent:changes-needed`) or to
   steer the roadmap.

To restore a human merge gate instead, delete `.github/workflows/auto-merge.yml`
(and/or add branch protection requiring CODEOWNERS review).

## 9. Current status

**Phase 0 — repository and agent setup.** Skeleton + agent OS only; the webhook
product is built phase-by-phase via the roadmap. No product logic is implemented
yet beyond the health endpoint.

## License

MIT.
