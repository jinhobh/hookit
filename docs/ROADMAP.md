# Roadmap — Reliable Webhook Delivery Platform

The product is built in small, issue-sized increments. Each phase lists tasks
that map roughly one-to-one to GitHub issues. The Planner agent keeps the
`agent:ready` queue stocked from this roadmap; the Builder agent implements one
issue per PR. Keep tasks small enough to review in a single sitting.

Legend: each task is sized to be a single PR with tests and passing checks.

---

## Phase 0 — Repo and agent setup ✅ (this PR)
- [x] Repository skeleton (FastAPI `/health`, config, tests).
- [x] Tooling: `pyproject.toml` (ruff, mypy, pytest), `docker-compose.yml`,
      `.env.example`, `.gitignore`.
- [x] Engineering docs: spec, architecture, roadmap, agent workflow, quality bar.
- [x] Agent OS: `CLAUDE.md`, `agents/*.md`, planner/builder/reviewer workflows, CI.
- [x] Labels and the first batch of issues.

## Phase 1 — FastAPI skeleton and config ✅
- [x] Add an app factory and structured logging setup.
- [x] Flesh out `app/core/config.py` settings consumed by the app (and a config
      smoke test).
- [x] Add a `Dockerfile` and wire the compose `app` service to actually build/run.
- [x] Add a `Makefile` or task runner documenting the standard commands.

## Phase 2 — Database and migrations ✅
- [x] Add SQLAlchemy 2.x engine/session factory and a declarative `Base`.
- [x] Initialize Alembic (env, script template) wired to `DATABASE_URL`.
- [x] Add a DB-touching test harness using the Postgres service.

## Phase 3 — Projects and API keys ✅
- [x] Model `projects` and `api_keys` (+ migration).
- [x] `POST /projects` and `POST /projects/{id}/api-keys` (hashed keys, plaintext
      returned once).
- [x] Implement API-key authentication dependency (hash lookup, project scoping,
      revocation).

## Phase 4 — Webhook endpoint registration ✅
- [x] Model `endpoints` (url, event types, secret, status) (+ migration).
- [x] CRUD: `POST/GET/PATCH/DELETE /endpoints`, scoped to the authed project.
- [x] Validate URLs and event-type subscriptions.

## Phase 5 — Event publishing and idempotency ✅
- [x] Model `events` and idempotency records (+ migration).
- [x] `POST /events`: auth → idempotency check → store event → fan-out to matching
      endpoints (creates delivery rows) → return event id + queued count.
- [x] Idempotency edge cases: same key/same body replay; same key/different body
      rejection.

## Phase 6 — Delivery records and attempt logs ✅
- [x] Model `deliveries` and `delivery_attempts` (+ migration; indexes).
- [x] Inspection APIs: `GET /events/{id}`, `GET /events`, `GET /deliveries`,
      `GET /deliveries/{id}`, `GET /deliveries/{id}/attempts`.

## Phase 7 — Worker delivery loop ✅
- [x] Claim model: `FOR UPDATE SKIP LOCKED` + leases (`leased_until`).
- [x] Worker loop: claim due deliveries, POST payload, record attempt, mark
      success/failure.
- [x] HMAC-SHA256 signing of outbound requests (timestamp + body).
- [x] Tests against a local in-process receiver (no external network).

## Phase 8 — Retries and dead-letter state ✅
- [x] Pure backoff policy (`base * 2 ** (attempt-1)`, capped, jittered) + unit
      tests.
- [x] Wire retries into the worker; schedule `next_attempt_at`.
- [x] Dead-letter on exhaustion; lease-expiry recovery for crashed workers.

## Phase 9 — Manual redrive ✅
- [x] `POST /deliveries/{id}/redrive` resets a dead-lettered delivery to pending.
- [x] Preserve attempt history; guard against redriving non-dead-lettered rows.

## Phase 10 — Demo receiver and README polish
- [ ] A tiny demo receiver app that verifies signatures (for end-to-end demo).
- [x] End-to-end happy-path test: publish → deliver → inspect.
- [x] README/architecture polish, sequence diagrams, and a short demo script.

---

### Beyond MVP (future extensions, not scheduled)
External broker (Redis/SQS/Kafka), `LISTEN/NOTIFY`-driven worker, Prometheus/
OpenTelemetry metrics, rate limiting per endpoint, a frontend dashboard. None of
these are worked on without an explicit issue and a human decision.
