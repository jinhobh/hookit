# producer — live crypto price producer

A **separate, standalone service** that makes the platform demo real. It polls a
real, keyless public crypto price API (Coinbase v2 spot), turns each observation
into a `price.tick` / `price.alert` event, and publishes it to the platform's
real public ingestion API (`POST /events`) with a real API key — exactly like an
external customer would. The platform then delivers those events to a real
Discord channel (and to a controllable receiver used for the reliability demo).

Nothing here touches the platform's database or internals; it is a genuine
outside producer.

## Run it

```bash
export PLATFORM_API_URL="http://localhost:8000"
export PLATFORM_API_KEY="whk_...the showcase project's key..."
python -m producer
```

The service:

- runs a **background poll loop** publishing events on an interval, and
- exposes a tiny **control server** on `CONTROL_PORT` (default 8100):
  - `GET  /health` — liveness.
  - `POST /burst`  — fire a rapid batch of tick events (a traffic spike). The
    platform dashboard reaches this via a same-origin proxy
    (`POST /showcase/burst`), so the control server need not be public.

## Configuration (environment variables)

| Variable                 | Default                              | Meaning                                            |
| ------------------------ | ------------------------------------ | -------------------------------------------------- |
| `PLATFORM_API_URL`       | `http://localhost:8000`              | Platform base URL for `POST /events`.              |
| `PLATFORM_API_KEY`       | _(empty)_                            | Bearer key for the seeded showcase project. Secret.|
| `PRICE_API_URL`          | `https://api.coinbase.com/v2/prices` | Keyless spot-price API base.                       |
| `SYMBOLS`                | `BTC-USD,ETH-USD,SOL-USD,DOGE-USD`   | Comma-separated product ids to poll.               |
| `POLL_INTERVAL_SECONDS`  | `4.0`                                | Seconds between polling cycles.                    |
| `ALERT_THRESHOLD_PCT`    | `0.5`                                | Percent move from anchor that triggers an alert.   |
| `BURST_COUNT`            | `20`                                 | Events fired per `/burst`.                          |
| `CONTROL_HOST`/`PORT`    | `0.0.0.0` / `8100`                   | Bind address of the control server.                |

## Tests

Pure logic and the HTTP clients are covered without any network:
`tests/test_producer_prices.py` and `tests/test_producer_client.py`.
