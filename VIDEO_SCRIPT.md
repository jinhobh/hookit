# HookIt — Demo & Explainer Script (~6–7 min)

**Format:** Each section has a **SPOKEN** block (what you say) and an **ON SCREEN** block (commands / files / actions). Placeholders and things to verify are marked `⚠️`. A consolidated verify-list is at the very bottom.

---

## 1. Hook (~30s)

**SPOKEN**
> So here's the problem this whole project exists to solve. When something happens in your app — a customer pays, an order ships — you need to tell some *other* system about it. The naive version is: the event fires, you make an HTTP POST, done. And that works right up until the receiver is down, or slow, or returns a 500, or the network drops the response *after* they already processed it. Now you've either lost the event or sent it twice, and you have no record either way. HookIt is the thing that sits in the middle and makes that delivery actually reliable — retries, backoff, dead-lettering, signing — so "fire and hope" becomes "fire and guarantee."

**ON SCREEN**
- Open `README.md` top section (lines 1–19) — the one-line pitch + the four guarantees.
- Optionally: a simple slide/whiteboard — `App → [ ??? ] → Receiver` with the receiver flickering "down."

---

## 2. Live demo (~90s)

You have two ways to run this. **Lead with the hosted dashboard** (most visual — it has literal buttons for take-down / redrive on live traffic), then show the raw API to prove it's a real service, not a UI trick.

**SPOKEN**
> Let me show it actually running. This dashboard is live — there's a separate producer service polling real crypto prices and publishing them as events, and they get delivered, HMAC-signed, into a real Discord channel. Watch: I'll take the receiver *down* — one button — and now deliveries start failing. You can see the attempts stacking up, each retry pushed further out by exponential backoff. If it never recovers, it lands here in the dead-letter queue. Now I bring the receiver back up, hit redrive, and the same delivery goes straight through — no data lost, it was just waiting. And to prove this isn't smoke and mirrors, here's the same thing from the raw API: I publish an event with curl, and I can query exactly what the worker did with it.

**ON SCREEN**
- Browser: `https://hookit.fly.dev/dashboard/` — narrate live traffic landing in the embedded Discord widget.
- Click the **take receiver down** button → point at the deliveries flipping to failing / retrying, attempt counts climbing.
- Show a delivery reaching **dead-lettered**, bring receiver back up, click **redrive** → show it succeed.
- Then drop to a terminal for the raw-API proof (exact commands):

```bash
BASE=https://hookit.fly.dev
KEY=whk_xxx   # ⚠️ your provisioned key — see §3 note below

# publish an event (Idempotency-Key makes a client retry safe)
curl -s -X POST $BASE/events -H "Authorization: Bearer $KEY" \
  -H 'Idempotency-Key: demo-1' -H 'Content-Type: application/json' \
  -d '{"type":"demo.ping","payload":{"hello":"world"}}'
# -> {"event_id":"...","queued_deliveries":1}

# inspect exactly what the worker did
curl -s $BASE/deliveries -H "Authorization: Bearer $KEY"
```

> ⚠️ **Note on key provisioning:** minting a project/API key is a bootstrap/admin step (`app/routers/projects.py` — `POST /projects`, then `POST /projects/{id}/api-keys`). Have `KEY` ready *before* recording; don't fumble it live.

**Fully-local alternative (most reproducible — no hosting needed):** the repo ships self-contained scenario scripts. This is the cleanest "exact commands" path if you'd rather demo offline:

```bash
docker compose up -d postgres && alembic upgrade head
uvicorn app.main:app --reload            # separate terminal
python -m demo.scenario_1_failure_backoff_deadletter   # 500s → backoff → dead-letter
python -m demo.scenario_2_redrive                       # dead-letter → redrive → success
```
Each script provisions its own project/key/endpoint, runs a local receiver + worker subprocess, and prints the observed attempt/delivery states (`demo/README.md`).

---

## 3. Architecture (~60s)

**SPOKEN**
> Architecturally it's four pieces. There's the **API** — FastAPI — that authenticates the client, checks idempotency, and fans the event out into delivery rows. There's **Postgres**, which is both my database *and* my job queue — I'll come back to why that matters. There's the **worker**, a completely separate process that claims delivery rows and does the actual outbound HTTP. And then the **receivers**, which are other people's endpoints I don't control. The key decision here is that the worker is its own process, not a background thread inside the API. Ingestion and delivery are decoupled on purpose: a slow or dead receiver can never block someone from publishing an event. The API's job is to durably write the event and return; the worker's job is to keep hammering on delivery in the background. They only ever talk through the database.

**ON SCREEN**
- Open `README.md` Mermaid diagram (lines 43–60) — trace `Client → API → Postgres → Worker → Receiver`.
- Open `app/worker/__main__.py` — point at the standalone `python -m app.worker` entrypoint (lines 1–9) to show it's a separate process.
- Optionally show `docs/ARCHITECTURE.md` §10 Tradeoffs briefly.

---

## 4. Deep dives (~3 min)

Three genuinely hard parts, all straight from the code.

### 4a. Postgres as a queue — `FOR UPDATE SKIP LOCKED` + leases (~60s)

**SPOKEN**
> First one: I used Postgres itself as the queue instead of reaching for Redis or SQS or Kafka. The reason is transactional fan-out — when an event comes in, the event row, all its delivery rows, and the idempotency record all get written in *one* commit. So it's impossible to store an event without also enqueuing its deliveries; there's no window where they can disagree. The tricky part is letting multiple workers pull work without stepping on each other. That's this query — `SELECT ... FOR UPDATE SKIP LOCKED`. Each worker grabs a batch of due deliveries and locks those rows; any other worker just *skips* the locked ones instead of blocking. So two workers naturally claim disjoint sets. I actually have a test that opens two real database sessions and asserts they claim completely non-overlapping deliveries. The other half is crash safety: when a worker claims a row it stamps a *lease* — `leased_until`, sixty seconds out. If that worker dies mid-delivery, the row is stuck IN_FLIGHT forever... unless something resets it. So every tick, before claiming, I sweep expired leases back to PENDING. A dead worker's work gets picked up by the next one automatically.

**ON SCREEN**
- `app/worker/delivery_worker.py` — `claim_due_deliveries` (lines 44–89), highlight `.with_for_update(skip_locked=True)` (line 75) and the PENDING→IN_FLIGHT + lease stamping (lines 81–84).
- `_recover_expired_leases` (lines 92–103) — the sweep back to PENDING.
- `tests/test_worker.py::test_concurrent_sessions_claim_disjoint_sets` (lines 289–324) — show `ids_1.isdisjoint(ids_2)`.
- ⚠️ Say it accurately: multi-worker concurrency is demonstrated via **multiple threads in one process, each with its own DB session** (`worker_concurrency`, `app/worker/__main__.py:101–119`) — the claim model *supports* multiple machines, but the demo runs concurrent sessions, not a multi-node cluster.

### 4b. Idempotency and the savepoint race (~70s) — *your strongest "hard bug" story*

**SPOKEN**
> Second, idempotency — and this is the part I'm most proud of because the bug is so sneaky. The client sends an `Idempotency-Key`. Same key, same body — you get back the cached result, no double-send. Same key, *different* body — you get a 409, because that's a client mistake. Easy enough on paper. The hard case is two identical requests arriving at the exact same time. Both check "does this key exist?", both see no, both try to insert. One wins; the loser hits the unique constraint. So I wrap the inserts in a savepoint — the loser's insert rolls back to the savepoint, I re-read the winner's record, and return *its* answer. The outer transaction survives.
>
> But here's the gotcha that actually bit me. In SQLAlchemy, running a query *autoflushes* any pending objects first. So if I'd built the Event object and *then* run the SELECT that finds the fan-out endpoints, that Event would get flushed into the outer transaction — it would escape the savepoint, and when the loser rolled back, the Event would *survive* as an orphan row with no idempotency record pointing at it. `begin_nested()` has the same trap: it flushes already-pending objects before it opens the savepoint. So the fix was all about *ordering* — do the endpoint query first, then open the savepoint, and only *then* create the Event and deliveries, so all three inserts live inside the savepoint and roll back together.

**ON SCREEN**
- `app/services/event_ingestion.py` — walk it top to bottom:
  - the first lookup + same-body/different-body branch (lines 57–71),
  - the comment explaining why the endpoint SELECT must run *before* adding pending objects (lines 77–89),
  - `session.begin_nested()` + the three `session.add`s inside it (lines 116–129),
  - the `except IntegrityError` → rollback → re-read → return cached (lines 130–147).
- ⚠️ **PLACEHOLDER — tell the truth about how you found it:** The code is written *defensively* against the orphan-row race and the comments describe the mechanism, but the history doesn't record a specific failure. Fill in honestly: *[PLACEHOLDER: did you actually observe an orphan Event row / a flaky idempotency test, or did you reason this out ahead of time from knowing SQLAlchemy's autoflush behavior?]* Say whichever is true — both are legitimate, but don't claim a production incident that didn't happen.

### 4c. Exponential backoff, dead-lettering, and a real deploy bug (~50s)

**SPOKEN**
> Third: what happens when delivery *fails*. Every failed attempt gets recorded as its own row — status, response body, duration, which worker tried it — so there's a full audit trail. The retry schedule is exponential backoff with jitter: delay is base times two-to-the-attempt, capped, plus a random jitter on top. The jitter matters more than it looks — without it, if a receiver has an outage and a thousand deliveries all fail at once, they'd all retry at the *exact* same second and hammer it back down the moment it recovers. Jitter smears them out. After the max attempts, the delivery is dead-lettered — it stops retrying and waits for a human to inspect or redrive it. And redrive is dead simple: flip it back to PENDING with next-attempt-now, and the worker picks it up like any other row.
>
> One real bug worth mentioning here, since it's the kind of thing that only shows up in production: my migrations used `sa.Enum` with `create_type=False`, and it turns out that flag is *silently ignored* on the generic Enum — only `postgresql.ENUM` honors it. So on a fresh database, Postgres tried to create the enum type twice and the deploy blew up with "type already exists." Every one of my tests passed, because the test fixtures build the schema with `create_all`, not the actual migrations — so nothing exercised the migration path until I did a real Fly.io deploy. The fix was a two-line type change, but I added a test that runs the full `alembic upgrade head` against a clean schema so it can never regress silently again.

**ON SCREEN**
- `app/worker/backoff.py` (whole file, 9–20) — `min(base * 2**(attempt-1), cap) + uniform(0, delay*0.1)`.
- `app/worker/delivery_worker.py::process_delivery` — the success / dead-letter / retry branch (lines 200–240); DeliveryAttempt row insert (lines 185–195).
- `app/routers/deliveries.py::redrive_delivery` (lines 138–153) — the "not dead-lettered → 409" guard and the flip to PENDING.
- The bug: `git show fb4485f` — read the commit message aloud; open `tests/test_migrations.py` as the regression test.
- ⚠️ **VERIFY the numbers you say out loud:** production defaults are `max_delivery_attempts=6`, `retry_base_seconds=10`, `retry_cap_seconds=3600` (`app/core/config.py:74–85`). The **demo scenarios** use different values (base ~3s, 3 attempts). Don't mix them up on camera — say "in production, 6 attempts starting at 10 seconds" and "the demo compresses that so it's watchable."

*(Optional bonus beat if you have time — a nice security-judgment detail):* you set `httpx` logging to WARNING in `app/worker/__main__.py:30–33` specifically because httpx logs full outbound URLs at INFO, and a Discord webhook URL has a token embedded in it — so INFO logging would leak a secret. Small, but it shows you were thinking about it.

---

## 5. What I'd change for production (~45s)

**SPOKEN**
> Honest limitations. The big one is Postgres-as-a-queue. It's genuinely great up to a point — transactional fan-out, no extra infrastructure, and `SKIP LOCKED` gets you real throughput. But it's a deliberate *early* choice, not a forever one. At high scale you're putting queue-shaped write and lock contention on the same database that holds your data, and that's exactly when you'd move deliveries onto something built for it — SQS or Kafka. I'd want to actually *measure* the limit before making that call, though, not cargo-cult a broker on day one. The other honest gap is delivery ordering — this is at-least-once with no ordering guarantee, which is the right call for webhooks but something a consumer has to know. And the multi-worker story is proven with concurrent sessions and threads; a real deployment would run it as separate machines behind the same claim model.

**ON SCREEN**
- `docs/ARCHITECTURE.md` §10 Tradeoffs (lines 145–157) on screen.
- Optionally the README "Key design decisions" (lines 65–79): Postgres-as-queue, at-least-once, backoff+jitter, SSRF.
- ⚠️ Do **not** claim a specific throughput number on camera unless you re-run `benchmark/` and read the fresh number — there's a benchmark harness, but only cite a figure you just measured.

---

## 6. Close (~20s)

**SPOKEN**
> So that's HookIt — a webhook delivery service that treats "did this actually arrive?" as the whole problem instead of an afterthought. The stuff I care about here isn't the endpoints, it's the boring-hard parts: making concurrent workers safe, making idempotency survive a race, making failure recoverable instead of silent. Code's on GitHub, there's a live instance you just watched, and every one of these guarantees has a test behind it. Thanks for watching.

**ON SCREEN**
- Live dashboard `https://hookit.fly.dev/dashboard/` + `/docs`.
- Repo root / README.
- Optionally run `pytest` and let the green pass scroll.

---

## ⚠️ Verify-before-recording checklist

1. **The idempotency story (§4b)** — decide the *true* framing: bug you observed vs. race you designed against defensively. The code and comments support "designed against"; there's no committed evidence of an actual incident. Fill the `[PLACEHOLDER]`.
2. **Retry numbers (§4c/§2)** — production: **6 attempts, base 10s, cap 3600s**. Demo scenarios use ~3s base / 3 attempts. State which you're showing.
3. **"Multiple workers" (§4a/§5)** — accurate phrasing: concurrent **sessions/threads in one process** demonstrate `SKIP LOCKED`; multi-machine is *supported*, not what's running.
4. **The migration bug (§4c)** — this one *is* confirmed real (commit `fb4485f`, caught during a manual Fly deploy). Safe to tell as a true war story.
5. **Throughput/benchmark claims (§5)** — only cite a number you just re-ran from `benchmark/`.
6. **API key for the live curl demo (§2)** — provision `KEY` before recording; project/key creation is a bootstrap step, not a public endpoint.
7. **`docs/ARCHITECTURE.md` has some stale `(future)` wording** (e.g. line 42 calls `app/worker/` future; §9 says "single-process polling loop in the MVP") — that predates the worker + LISTEN/NOTIFY actually shipping. Don't read those lines verbatim as current state.
8. **Live instance scales to zero** — first request after idle takes a second or two (README:31–33). Warm it up right before recording so the demo isn't waiting on a cold start.
