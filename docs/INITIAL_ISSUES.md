# Initial Issues

The first 8 issues seeded into the queue (Phase 1–7 of
[`ROADMAP.md`](ROADMAP.md)). If `gh` is authenticated these are created
automatically by the setup; otherwise create them by hand with the titles,
bodies, and labels below. Each follows the Planner's required format so future
issues match the bar.

> Labels used below assume the label set in this repo exists (see the bottom of
> this file for the full list).

---

## Issue 1 — Set up FastAPI config and environment loading

**Labels:** `type:setup`, `risk:low`, `agent:ready`

**Goal:** Establish the application's configuration layer so all settings flow
through Pydantic Settings and the app starts cleanly from environment/`.env`.

**Context:** Phase 1 of the roadmap. `app/core/config.py` exists as a skeleton;
this hardens it as the single source of configuration consumed by the app.

**Acceptance criteria:**
- `Settings` loads from environment and `.env`, with sane typed defaults for
  `app_name`, `app_env`, `debug`, and `database_url`.
- A cached accessor (`get_settings()`) is used by `app/main.py`.
- A test verifies settings load and that env overrides take effect.
- No secrets hardcoded; `.env.example` documents every variable.

**Implementation notes:** Keep it minimal — no DB connection yet. Follow
`CLAUDE.md` §8 (config rules). Out of scope: engine/session, models.

**Verification commands:**
```bash
ruff format --check . && ruff check . && mypy app tests && pytest
```

---

## Issue 2 — Add SQLAlchemy engine/session setup and Alembic migrations

**Labels:** `type:feature`, `risk:medium`, `agent:ready`

**Goal:** Provide a SQLAlchemy 2.x engine + session factory and an initialized
Alembic environment wired to `DATABASE_URL`.

**Context:** Phase 2. Foundation for all persistence. Depends on Issue 1.

**Acceptance criteria:**
- Declarative `Base` and a session/engine factory live under `app/db/`.
- Alembic is initialized (`migrations/`, `alembic.ini`, `env.py`) and reads
  `DATABASE_URL` from settings.
- `alembic upgrade head` runs against the compose Postgres (document how).
- A test connects to the Postgres service and executes `SELECT 1`.

**Implementation notes:** Use psycopg v3 (`postgresql+psycopg://`). No models in
this issue beyond an empty initial migration. Schema changes only via Alembic
(`CLAUDE.md` §8).

**Verification commands:**
```bash
docker compose up -d postgres
ruff format --check . && ruff check . && mypy app tests && pytest
alembic upgrade head
```

---

## Issue 3 — Model projects and API keys

**Labels:** `type:feature`, `risk:medium`, `agent:ready`

**Goal:** Add `projects` and `api_keys` tables with a migration and ORM models.

**Context:** Phase 3. API keys must be stored hashed; plaintext returned once at
creation. Depends on Issue 2.

**Acceptance criteria:**
- `Project` and `ApiKey` models + Alembic migration (FK, indexes, unique
  `key_hash`).
- A helper generates a high-entropy key, returns the plaintext once, and stores
  only its hash (+ a short prefix for lookup/display).
- Unit tests cover key generation/hashing and model persistence.

**Implementation notes:** Do not add endpoints in this issue (that's Issue 4's
auth + the projects endpoints can be a follow-up). Never log plaintext keys.

**Verification commands:**
```bash
docker compose up -d postgres
ruff format --check . && ruff check . && mypy app tests && pytest && alembic upgrade head
```

---

## Issue 4 — Implement API key authentication

**Labels:** `type:feature`, `risk:high`, `agent:ready`

**Goal:** Authenticate requests via `Authorization: Bearer <api_key>`, resolving
to the owning project.

**Context:** Phase 3. Security-critical; gates all client APIs. Depends on
Issue 3.

**Acceptance criteria:**
- A FastAPI dependency extracts the bearer token, looks up the key by hash,
  rejects missing/invalid/revoked keys with 401, and yields the project.
- Constant-time comparison; no secret values logged.
- `last_used_at` is updated (or a clear note on why deferred).
- Tests cover valid, missing, malformed, and revoked keys.

**Implementation notes:** Add a minimal protected probe route (e.g.
`GET /me` returning the project id) to exercise the dependency. Follow
`CLAUDE.md` §9.

**Verification commands:**
```bash
docker compose up -d postgres
ruff format --check . && ruff check . && mypy app tests && pytest
```

---

## Issue 5 — Model webhook endpoints

**Labels:** `type:feature`, `risk:medium`, `agent:ready`

**Goal:** Add the `endpoints` table and registration/management API scoped to the
authenticated project.

**Context:** Phase 4. Depends on Issue 4.

**Acceptance criteria:**
- `Endpoint` model + migration (url, `event_types`, secret, status, timestamps).
- `POST /endpoints`, `GET /endpoints`, `PATCH /endpoints/{id}`,
  `DELETE /endpoints/{id}`, all project-scoped and authenticated.
- URL and event-type validation via Pydantic; secret stored securely (not
  plaintext in logs).
- Tests cover create/list/update/delete and cross-project isolation.

**Implementation notes:** Keep signing-secret handling consistent with the
architecture doc. No delivery logic yet.

**Verification commands:**
```bash
docker compose up -d postgres
ruff format --check . && ruff check . && mypy app tests && pytest
```

---

## Issue 6 — Implement event ingestion endpoint

**Labels:** `type:feature`, `risk:high`, `agent:ready`

**Goal:** Implement `POST /events` with authentication, idempotency, event
storage, and fan-out to matching endpoints.

**Context:** Phase 5 — the heart of the API. Depends on Issues 4, 5, and 7's
schema (coordinate ordering; deliveries may be created as rows even before the
worker exists).

**Acceptance criteria:**
- Authenticates; reads `Idempotency-Key`; stores the `Event`.
- Finds endpoints subscribed to the event type and creates one delivery row per
  match (status `PENDING`).
- Returns `{ event_id, queued_deliveries }`.
- Replaying the same key+body returns the same result without duplicates; same
  key + different body is rejected (409). All in one transaction.
- Tests cover happy path, idempotent replay, and conflict.

**Implementation notes:** Event + deliveries + idempotency record committed
atomically (`ARCHITECTURE.md` §7). Payload size/schema bounded.

**Verification commands:**
```bash
docker compose up -d postgres
ruff format --check . && ruff check . && mypy app tests && pytest
```

---

## Issue 7 — Create delivery and delivery_attempt schema

**Labels:** `type:feature`, `risk:medium`, `agent:ready`

**Goal:** Add `deliveries` and `delivery_attempts` tables with migrations and the
indexes needed to claim due work.

**Context:** Phase 6. Foundation for the worker and inspection APIs. Depends on
Issues 5 and 6 (event/endpoint FKs).

**Acceptance criteria:**
- `Delivery` (status, attempt_count, next_attempt_at, leased_until, FKs) and
  `DeliveryAttempt` (attempt_number, response status/body excerpt, error,
  duration) models + migration.
- Index supporting `status = PENDING AND next_attempt_at <= now()`.
- Read APIs: `GET /deliveries`, `GET /deliveries/{id}`,
  `GET /deliveries/{id}/attempts`, `GET /events/{id}`.
- Tests cover model persistence and the list/detail endpoints.

**Implementation notes:** No worker yet — just schema + inspection. Align state
machine with `PROJECT_SPEC.md` §8.

**Verification commands:**
```bash
docker compose up -d postgres
ruff format --check . && ruff check . && mypy app tests && pytest
```

---

## Issue 8 — Implement first delivery worker loop

**Labels:** `type:feature`, `risk:high`, `agent:ready`

**Goal:** A worker that claims due deliveries, POSTs the signed payload, records
an attempt, and marks success/failure (retries can be a follow-up).

**Context:** Phase 7. Depends on Issue 7.

**Acceptance criteria:**
- Claim logic uses `FOR UPDATE SKIP LOCKED` + a lease (`leased_until`).
- Outbound POST is signed with HMAC-SHA256 (timestamp + body) using the
  endpoint secret; timeout enforced.
- Every attempt writes a `DeliveryAttempt`; 2xx → `SUCCEEDED`, failure → recorded
  (retry scheduling may be a separate Phase 8 issue).
- Tests run against a local in-process receiver (no external network) and verify
  signing and attempt recording.

**Implementation notes:** Keep backoff/dead-letter for Phase 8. Worker is a
separate entrypoint (`app/worker/`). No secrets in logs.

**Verification commands:**
```bash
docker compose up -d postgres
ruff format --check . && ruff check . && mypy app tests && pytest
```

---

## Label set

Lifecycle: `agent:ready`, `agent:in-progress`, `agent:needs-review`,
`agent:changes-needed`, `agent:approved`.
Risk: `risk:low`, `risk:medium`, `risk:high`.
Type: `type:setup`, `type:feature`, `type:bug`, `type:refactor`, `type:docs`,
`type:test`.
