# Showcase Plan — Making the Hard Parts Visible

This plan upgrades the live showcase (the `/dashboard/` + producer + Discord
demo) from "watch webhooks arrive" to a demonstration of the platform's actual
engineering: exponential backoff with jitter, race-free concurrent claiming,
idempotency, crash recovery — and, most importantly, **what those guarantees
buy a downstream consumer whose money is on the line**.

Terminology: the *showcase* is the hosted live demo (`app/services/showcase.py`,
`app/routers/showcase.py`, `producer/`, `app/static/index.html`). The local
scripted scenarios in `demo/` are unaffected.

Like the roadmap, each task below is sized to one issue → one PR with tests and
passing checks. Phases map one-to-one to issues unless noted.

---

## Design principles

1. **Never script an animation.** Every panel observes the real production
   paths (`ingest_event`, `claim_due_deliveries`, `process_delivery`, real HMAC
   signing); every button pushes the real engine into a real failure state.
2. **Public reads, hard-scoped writes.** All new routes live under
   `/showcase/*` and follow the existing pattern: unauthenticated, but scoped
   to the single seeded showcase project so no customer data is reachable.
3. **The viewer is a stranger.** Every chaos button gets one on-screen sentence
   explaining what just broke and which platform guarantee handled it.

---

## The two demo layers

**Layer 1 — the plumbing view (phases D1–D4).** Makes the platform's internal
mechanics observable: retry timelines with live countdowns, per-worker claim
attribution, idempotent ingestion, lease recovery.

**Layer 2 — the ledger view (phases L1–L5).** The centerpiece. A webhook
platform cannot itself stop two writers from corrupting a ledger — it sits
upstream of that. What it provides is **at-least-once delivery, a stable event
id, a signed timestamp, and an attempt counter** on every delivery. Those are
exactly the primitives a receiver needs to stay correct under duplicates,
concurrency, disorder, and forgery. Layer 2 builds a live victim to prove it:

> **One event stream, two banks.** The producer emits `trade.executed` events
> against a few demo accounts. HookIt fans each trade out to two receiver
> applications shown side by side. **Bank A** (naive) applies every webhook as
> it arrives: read balance, add, write — no dedupe, no locking, no signature
> check. **Bank B** (correct) verifies the HMAC, records each `event_id` in a
> processed-events table, and applies changes atomically under a row lock.
> A **reconciliation meter** continuously diffs both ledgers against the
> expected balances computed from the platform's own event log (the source of
> truth) and displays drift in dollars. Visitors get chaos buttons; Bank A
> bleeds money, Bank B stays solvent.

### The chaos scenarios (all real, none simulated)

| Button | Failure injected | What Bank A does | What Bank B does | Guarantee shown |
|---|---|---|---|---|
| **Lost ack** | Banks process the webhook, then fail to respond (flaky mode) → platform correctly retries | Credits the trade twice; balance drifts | Sees `event_id` already processed, answers 200, unchanged | At-least-once + stable event ids; dedupe is the receiver's job |
| **Two writers, one account** | Burst of concurrent trades on the same account via multiple workers | Read-modify-write interleaves; a trade evaporates | Row lock / atomic update; exact balance | Concurrency safety is built from delivery metadata + DB primitives |
| **Time travel** | Pipeline down → retries arrive after newer live events | Stamps last-write-wins fields by arrival order; displays stale state | Compares signed event timestamps; rejects stale overwrites | Ordering is not guaranteed; timestamps make disorder survivable |
| **The forger** | Browser POSTs an unsigned fake trade directly to both banks | Balance rockets by the forged amount | 401, request shown rejected | HMAC-SHA256 signing + constant-time verification |
| **Reset** | — | Rebuilt from the event log | Rebuilt from the event log | The durable event log makes any consumer recoverable by replay |

---

## Layer 1 — plumbing view

### Phase D1 — Delivery lifecycle timeline (backoff + jitter visible)
- [x] `GET /showcase/deliveries`: the receiver endpoint's recent deliveries
      with `status`, `attempt_count`, `next_attempt_at`, `leased_until`, and
      each delivery's `attempts[]` (number, HTTP status, duration_ms,
      created_at). Read-only, scoped like `/showcase/feed`.
- [x] Dashboard "Delivery lifecycle" panel: per-delivery attempt timeline with
      measured gaps labeled, live countdown to `next_attempt_at`, and the
      formula annotated per gap: nominal `min(base·2^(n−1), cap)` vs observed
      gap — the difference **is** the jitter, shown explicitly.
- [x] Quick win, same PR as the panel: "Send forged request" link that POSTs an
      unsigned payload to the receiver URL from the browser → shows up in the
      inbox as `✗ unsigned → 401`.
- [x] Deploy tuning (env only, no code): `RETRY_BASE_SECONDS=5`,
      `RETRY_CAP_SECONDS=60`, `MAX_DELIVERY_ATTEMPTS=5` on Fly so a full
      retry → dead-letter cycle is watchable (~75 s). The fast-forward button
      remains the impatient path.

### Phase D2 — Multi-worker claiming (race handling visible)
- [x] Migration: `deliveries.claimed_by` (nullable text) and
      `delivery_attempts.worker_id`; stamped in `claim_due_deliveries` /
      `process_delivery`.
- [x] Settings: `WORKER_NAME` (default `hostname:pid`) and
      `WORKER_CONCURRENCY` — the worker entrypoint spawns N independent claim
      loops (own session + name each) in one process, exercising
      `FOR UPDATE SKIP LOCKED` across real sessions without extra machines.
- [x] Dashboard: color-code timeline items by worker (inbox rows carry no
      worker attribution — deliberately not added, since that would mean a new
      outbound header on the production delivery path); "Workers" strip with
      per-worker attempt counts; standing caption "0 duplicate attempts —
      enforced by `UNIQUE(delivery_id, attempt_number)`".
- [x] Test: two sessions claim concurrently → disjoint sets, correct
      `claimed_by`.

### Phase D3 — Idempotency race button
- [x] Producer control server: `POST /duplicate` fires the same payload twice
      **concurrently** with the same `Idempotency-Key` (a genuine race on the
      ingestion unique constraint, not a sequential replay).
- [x] `POST /showcase/duplicate` proxies to it (same pattern as `/burst`) and
      returns both responses.
- [x] Dashboard: "Publish duplicate" button; result shows both responses carry
      the same `event_id` and one delivery was created, not two.

### Phase D4 — Crash / lease recovery (stretch; requires D2)
- [ ] Receiver health gains a *slow* mode (healthy / slow ~8 s / down) so a
      delivery is reliably in flight when the crash hits.
- [ ] Worker control endpoint (private, like the producer's): `POST /crash` →
      `os._exit(1)` mid-batch; the supervisor restarts it.
- [ ] Dashboard: "Kill a worker" button; timeline shows `leased_until`
      counting down, lease recovery resetting the row to PENDING, and the
      surviving worker completing it.
- [ ] Do this phase only if D1–D2 land well; the D1 timeline already displays
      lease state, which is most of the story.

---

## Layer 2 — ledger view

### Phase L1 — Trades producer + ledger schema
- [ ] Producer emits `trade.executed` events (`account`, `side`, `amount`,
      `trade_id`, `executed_at`) for ~4 demo accounts alongside price events;
      amounts derived from real price moves so the stream stays live.
      New producer settings: `TRADE_INTERVAL_SECONDS`, `TRADE_ACCOUNTS`.
- [ ] Migration: `demo_ledger_accounts` (`bank`, `account`, `balance`,
      `status`, `status_as_of`) and `demo_ledger_processed` (`bank`,
      `event_id`, PK on both).
- [ ] Seeding: two new endpoints on the showcase project subscribing to
      `trade.executed` (Bank A, Bank B), created in `seed_showcase`.

### Phase L2 — The two banks
- [ ] Bank receiver routes in the app (same public-sink pattern as the
      existing controllable receiver, keyed off the `__showcase__` marker):
      - **Bank A** (`/showcase/ledger/naive/{endpoint_id}`): no signature
        check, no dedupe; deliberate read → sleep(~50 ms) → write so the
        lost-update race is reliably visible rather than a coin flip.
      - **Bank B** (`/showcase/ledger/safe/{endpoint_id}`): HMAC verification,
        `INSERT` into `demo_ledger_processed` + `SELECT … FOR UPDATE` balance
        update in one transaction; stale-timestamp guard on last-write-wins
        fields.
- [ ] Per-bank health extends the existing mechanism: healthy / **flaky**
      (process, then respond 500/timeout) / down.
- [ ] Deterministic race test: two sessions + a barrier prove Bank A loses an
      update and Bank B does not, against real Postgres.

### Phase L3 — Reconciliation + dashboard panel
- [ ] `GET /showcase/ledger`: both banks' balances, expected balances computed
      from the project's `trade.executed` event log, per-account drift in
      dollars, and each bank's recent received-request tail.
- [ ] Dashboard: side-by-side bank panel with green/red reconciliation meters
      and drift-in-dollars. This becomes the page's centerpiece; the existing
      retry/DLQ controls remain as the plumbing view.

### Phase L4 — Chaos buttons + copy
- [ ] `POST /showcase/ledger/health` (per-bank flaky/down toggle) → **Lost
      ack** scenario.
- [ ] Burst option `same_account=true` (producer + proxy) → **Two writers,
      one account** scenario (requires D2's concurrent workers).
- [ ] **Time travel** uses existing controls (pipeline down → retries → up);
      needs only the stale-overwrite display on the bank panel.
- [ ] **Forger** button (browser-side POST to both banks, no backend change).
- [ ] One sentence of on-screen copy per scenario: what broke, which guarantee
      Bank B used to survive it.

### Phase L5 — Stretch: replay recovery
- [ ] `POST /showcase/ledger/reset`: rebuild both banks by replaying the
      project's stored events in order through the real delivery path —
      the event log as a visible disaster-recovery mechanism.

---

## Sequencing

```
D1 ──► D2 ──► D3          (plumbing; D3 independent of D2 but ships after)
        │
        ├──► D4 (stretch)
        │
L1 ──► L2 ──► L3 ──► L4 ──► L5 (stretch)
        (L4's concurrency scenario needs D2)
```

Rationale: D1 is pure read-side (no migration, lowest risk, biggest visual
payoff). D2 introduces the only Layer-1 schema change and unlocks both D4 and
L4's race scenario. The ledger phases are additive and never touch the
production delivery path — the banks are consumers of it.

## Quality bar

Standard gates on every PR (`ruff format --check`, `ruff check`,
`mypy app tests`, `pytest`); DB-touching tests run against real Postgres, no
mocking the database away; outbound HTTP mocked or served by in-process
receivers. All new `/showcase/*` surface stays hard-scoped to the seeded
showcase project.

## The pitch the finished demo makes

> Here are two banks fed by the same event stream. Break the world — flood it,
> drop acks, pull the plug mid-delivery, forge requests, replay history — and
> watch the bank built on this platform's guarantees stay solvent while the
> other bleeds money. Nothing is simulated: every failure is real, every
> recovery is the production code doing its job.
