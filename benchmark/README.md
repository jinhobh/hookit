# Benchmark Harness

Measures the delivery worker's **throughput** (deliveries/sec) and
**end-to-end latency** (p50/p95/p99) from event creation to delivery success.

## Prerequisites

1. Postgres running and migrated:
   ```bash
   docker compose up -d postgres
   alembic upgrade head
   ```
2. The FastAPI app running:
   ```bash
   uvicorn app.main:app --reload
   ```

The benchmark starts the delivery worker automatically.

## Running

```bash
# Defaults: 500 events, concurrency=10
python -m benchmark

# Smaller run (good for a quick smoke-test)
python -m benchmark --events 200 --concurrency 10

# Larger run against a remote API
python -m benchmark --events 2000 --concurrency 20 --api-base http://myhost:8000
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--events N` | 500 | Total events to publish |
| `--concurrency C` | 10 | Concurrent publish threads |
| `--sink-port PORT` | auto | Port for the local sink; 0 = pick automatically |
| `--api-base URL` | `http://localhost:8000` | API base URL (or env `API_BASE_URL`) |
| `--timeout S` | 120 | Seconds to wait for all deliveries |

## Reading the Output

```
======================================================
  BENCHMARK RESULTS
======================================================
  api base   : http://localhost:8000
  sink       : http://localhost:54321/
  events     : 500  (concurrency=10)
  succeeded  : 500
  wall time  : 4.82s  (worker start → all delivered)
  throughput : 103.7 deliveries/sec

  metric                      value
  ---------------------- ----------
  latency p50               1234ms
  latency p95               2100ms
  latency p99               2800ms
  latency mean              1350ms
  latency min                890ms
  latency max               3100ms
======================================================

  Note: figures are single-worker on a dev box; the architecture
  supports horizontal scaling by running multiple worker processes.
```

**Throughput** is `deliveries / wall_time` where wall time is measured from
when the worker process starts until all deliveries reach `succeeded` status.

**End-to-end latency** is `delivery.updated_at − delivery.created_at` for each
succeeded delivery. `created_at` is set when the event is ingested (fan-out
happens synchronously inside `POST /events`); `updated_at` is set when the
worker marks the delivery `succeeded`.

## What the Sink Receiver Does

The harness reuses `tools/configurable_receiver.py` as a minimal sink
(`--status 200`) listening on localhost. It always returns HTTP 200 immediately,
so the receiver is never the bottleneck — the figures reflect the delivery
engine, not the downstream service.

The sink URL (`http://localhost:PORT/`) bypasses SSRF IP-literal checks
(a known limitation documented in `docs/ARCHITECTURE.md`), which is intentional
for this local-only benchmark tool.

## Caveats

- **Single-worker headline number.** The architecture supports horizontal
  scaling by running multiple `python -m app.worker` processes (each claims
  different rows via `FOR UPDATE SKIP LOCKED`). Run two or more workers in
  parallel to validate fan-out.
- **Dev-box figures.** Results vary with Postgres I/O, OS scheduling, and
  available CPU cores. For reproducible numbers, pin worker and DB to isolated
  cores.
- **No CI gate.** Benchmarks are not part of the CI pipeline; run them
  manually before quoting figures in documentation.
