# Deploying to Fly.io

A step-by-step guide to put the API **and** the delivery worker live on Fly.io,
backed by Postgres. Pairs with the repo-root [`fly.toml`](../fly.toml).

> You run these commands — they need *your* Fly account and a payment method on
> file (Fly requires a card even for the small/free allowances). Nothing here
> commits secrets to git; all secrets live in `fly secrets`.

The deploy runs **two process groups from one image**: `app` (FastAPI) and
`worker` (`python -m app.worker`). Schema migrations run automatically on every
deploy via `release_command = "alembic upgrade head"`.

---

## 0. One-time: install + log in

```bash
curl -L https://fly.io/install.sh | sh   # installs flyctl
fly auth login
```

## 1. Create the app

Pick a globally-unique name and use it everywhere below (replace `YOUR-APP`).

```bash
fly apps create YOUR-APP
```

Then set `app = "YOUR-APP"` at the top of `fly.toml` (and `primary_region` to a
region near you — `fly platform regions` lists them).

## 2. Create Postgres and wire the connection string

```bash
fly postgres create --name YOUR-APP-db --region iad   # match your app region
fly postgres attach YOUR-APP-db --app YOUR-APP         # creates a DB user + sets DATABASE_URL
```

`attach` sets a `DATABASE_URL` secret like
`postgres://user:pass@YOUR-APP-db.flycast:5432/yourdb`. **SQLAlchemy needs the
psycopg v3 driver**, so overwrite it with the `+psycopg` scheme (copy the
user/pass/host from the attach output):

```bash
fly secrets set --app YOUR-APP \
  DATABASE_URL="postgresql+psycopg://user:pass@YOUR-APP-db.flycast:5432/yourdb?sslmode=disable"
```

> `sslmode=disable` is fine over Fly's private `.flycast` network. *Alternative:*
> any managed Postgres (Neon/Supabase free tier) works too — just set
> `DATABASE_URL` to its connection string rewritten with `postgresql+psycopg://`.

## 3. Set the remaining secrets / config

The Fernet key encrypts endpoint signing secrets at rest. **Generate a real one**
(never use the dev default in `app/core/config.py`):

```bash
# generate a key (needs the local venv with `cryptography` installed)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

fly secrets set --app YOUR-APP \
  ENDPOINT_SECRET_KEY="<paste-the-generated-key>" \
  APP_ENV="production" \
  DEBUG="false"
```

> Setting secrets triggers a restart — that's expected. Config maps env → settings
> in `app/core/config.py` (e.g. `ENDPOINT_SECRET_KEY` → `endpoint_secret_key`).

## 4. Deploy

```bash
fly deploy --app YOUR-APP
```

This builds the image, runs `alembic upgrade head` in a release machine, then
starts the `app` and `worker` machines. Watch it:

```bash
fly logs --app YOUR-APP          # tail both processes
fly status --app YOUR-APP        # see app + worker machines
```

## 5. Verify it's live

```bash
curl https://YOUR-APP.fly.dev/health        # -> {"status":"ok"}
```

End-to-end smoke test (no UI — it's an API):

```bash
BASE=https://YOUR-APP.fly.dev

# NOTE: project/key provisioning is an admin/bootstrap step. Use the project's
# provisioning endpoints (see app/routers/projects.py) to mint a key, then:

KEY=whk_xxx   # the plaintext API key, shown once at creation

# register an endpoint pointing at a receiver you control
curl -s -X POST $BASE/endpoints -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://your-receiver.example/hook","event_types":["demo.ping"]}'
# -> returns the endpoint + its signing secret (shown once)

# publish an event (Idempotency-Key makes client retries safe)
curl -s -X POST $BASE/events -H "Authorization: Bearer $KEY" \
  -H 'Idempotency-Key: demo-1' -H 'Content-Type: application/json' \
  -d '{"type":"demo.ping","payload":{"hello":"world"}}'
# -> {"event_id": "...", "queued_deliveries": 1}

# inspect what the worker did
curl -s $BASE/deliveries -H "Authorization: Bearer $KEY"
```

To watch a signed webhook actually arrive, run the bundled receiver anywhere
publicly reachable and use its URL as the endpoint above:

```bash
python tools/demo_receiver.py --secret "<the-endpoint-signing-secret>" --port 8888
# verifies the X-Webhook-Signature (t=<ts>,v1=<hmac>) and returns 200 / 401
```

---

## Operating notes

- **Scale the worker** (more throughput): `fly scale count worker=2 --app YOUR-APP`
  — the `FOR UPDATE SKIP LOCKED` claim model makes multiple workers safe.
- **Cost control**: set `min_machines_running = 0` in `fly.toml` to let the `app`
  auto-stop when idle (adds a cold-start delay on the next request). The `worker`
  is a continuous poller, so it stays running.
- **Migrations only** (without a full redeploy), run them from your laptop:
  ```bash
  fly proxy 5432 -a YOUR-APP-db          # in one terminal
  DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/yourdb" alembic upgrade head
  ```
- **Secrets**: list with `fly secrets list` (values are hidden). Never paste a
  real key into a file that gets committed.

## Teardown

```bash
fly apps destroy YOUR-APP
fly apps destroy YOUR-APP-db
```
