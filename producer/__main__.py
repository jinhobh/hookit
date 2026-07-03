"""Entry point for the live crypto producer service.

Runs three things in one process:

1. A background **poll loop** that fetches real spot prices on an interval and
   publishes ``price.tick`` / ``price.alert`` events to the platform.
2. A background **trade loop** that emits ``trade.executed`` events against a
   few demo accounts, priced off the latest live observations — the event
   stream the two-banks ledger demo consumes.
3. A tiny **control server** exposing ``POST /burst`` (fire a rapid batch of
   tick events to demonstrate a traffic spike; with ``{"same_account": true}``
   it instead fires concurrent same-account trades — the "two writers, one
   account" chaos scenario), ``POST /duplicate`` (fire the same payload twice
   concurrently with one ``Idempotency-Key`` — a genuine race on the
   platform's ingestion unique constraint), and ``GET /health``. The
   platform's dashboard reaches these via same-origin proxies, so this server
   does not need to be publicly exposed.

Run with ``python -m producer``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from producer.client import PlatformClient, PriceSource
from producer.prices import PriceTracker, build_alert_event, build_tick_event
from producer.settings import ProducerSettings, get_producer_settings
from producer.trades import TradeGenerator

logger = logging.getLogger("producer")


async def _poll_loop(
    *,
    source: PriceSource,
    platform: PlatformClient,
    tracker: PriceTracker,
    symbols: list[str],
    interval: float,
) -> None:
    """Forever: fetch each symbol's spot price and publish its events.

    Resilient by design — a failed fetch or publish for one symbol is logged and
    skipped so a transient upstream hiccup never stops the stream.
    """
    logger.info("poll loop started: %d symbols every %.1fs", len(symbols), interval)
    while True:
        for symbol in symbols:
            try:
                price = await source.spot(symbol)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("skip %s: %s", symbol, exc)
                continue
            for event_type, payload in tracker.observe(symbol, price):
                await platform.publish(event_type, payload)
        await asyncio.sleep(interval)


async def _trade_loop(
    *,
    platform: PlatformClient,
    tracker: PriceTracker,
    trades: TradeGenerator,
    interval: float,
) -> None:
    """Forever: emit one ``trade.executed`` priced off the latest live tick.

    Waits for the poll loop's first observations rather than fetching itself;
    if no price has been seen yet the cycle is skipped, never crashed.
    """
    logger.info("trade loop started: one trade every %.1fs", interval)
    while True:
        latest = tracker.latest()
        if latest:
            symbol, price = random.choice(list(latest.items()))
            event_type, payload = trades.next_trade(symbol, _jittered(price))
            await platform.publish(event_type, payload)
        await asyncio.sleep(interval)


async def _fire_trade_burst(
    *,
    platform: PlatformClient,
    tracker: PriceTracker,
    trades: TradeGenerator,
    count: int,
) -> int:
    """Fire *count* trades against **one** account, all concurrently.

    The concurrent publishes fan out to concurrent worker loops, so the two
    banks receive overlapping deliveries for the same account — the naive
    bank's unlocked read-modify-write visibly loses an update while the safe
    bank's row lock keeps the exact balance. Returns how many were accepted;
    0 when no live price has been observed yet.
    """
    latest = tracker.latest()
    if not latest:
        return 0
    symbol, price = random.choice(list(latest.items()))
    events = trades.burst_same_account(symbol, _jittered(price), count)
    statuses = await asyncio.gather(*(platform.publish(t, p) for t, p in events))
    return sum(1 for s in statuses if 200 <= s < 300)


async def _fire_burst(
    *,
    platform: PlatformClient,
    tracker: PriceTracker,
    source: PriceSource,
    symbols: list[str],
    count: int,
) -> int:
    """Publish a rapid burst to simulate a traffic spike.

    Fires *count* ``price.tick`` events (which drive throughput and the
    controllable-receiver reliability demo) plus one ``price.alert`` per symbol
    (which the platform routes to Discord, so the channel visibly lights up the
    moment a visitor clicks). All are grounded in the latest observed prices with
    a small random jitter; if nothing has been observed yet, seeds from one live
    fetch. Returns the total number of events published.

    Publishes **concurrently** (``asyncio.gather``) so the whole spike completes
    in about one round-trip rather than the sum of ~two dozen sequential POSTs —
    which would otherwise exceed the caller's HTTP timeout.
    """
    latest = tracker.latest()
    if not latest:
        for symbol in symbols:
            try:
                latest[symbol] = await source.spot(symbol)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("burst seed skip %s: %s", symbol, exc)
    if not latest:
        return 0

    items = list(latest.items())
    events: list[tuple[str, dict[str, Any]]] = []
    for i in range(count):
        symbol, base_price = items[i % len(items)]
        etype, payload = build_tick_event(symbol, _jittered(base_price), base_price)
        payload["burst"] = True
        events.append((etype, payload))

    # A handful of alerts so the Discord channel shows life on demand.
    for symbol, base_price in items:
        etype, payload = build_alert_event(symbol, _jittered(base_price), base_price, Decimal("0"))
        payload["burst"] = True
        events.append((etype, payload))

    statuses = await asyncio.gather(*(platform.publish(t, p) for t, p in events))
    return sum(1 for s in statuses if 200 <= s < 300)


async def _fire_duplicate(
    *,
    platform: PlatformClient,
    tracker: PriceTracker,
    source: PriceSource,
    symbols: list[str],
) -> dict[str, Any]:
    """Fire the same tick twice **concurrently** with one ``Idempotency-Key``.

    A genuine race on the platform's ingestion unique constraint — both POSTs
    are in flight at once (``asyncio.gather``), so neither is a sequential
    replay of a committed record. Returns the key and both responses so the
    dashboard can show that they carry the same ``event_id`` and that only one
    delivery was created.
    """
    latest = tracker.latest()
    if not latest:
        for symbol in symbols:
            try:
                latest[symbol] = await source.spot(symbol)
                break
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("duplicate seed skip %s: %s", symbol, exc)
    if not latest:
        return {"idempotency_key": None, "results": []}

    symbol, base_price = next(iter(latest.items()))
    event_type, payload = build_tick_event(symbol, _jittered(base_price), base_price)
    payload["duplicate"] = True
    key = f"dup-{uuid.uuid4().hex}"

    pairs = await asyncio.gather(
        platform.publish_with_key(event_type, payload, key),
        platform.publish_with_key(event_type, payload, key),
    )
    return {
        "idempotency_key": key,
        "results": [
            {
                "status": status,
                "event_id": body.get("event_id"),
                "queued_deliveries": body.get("queued_deliveries"),
            }
            for status, body in pairs
        ],
    }


def _jittered(base_price: Decimal) -> Decimal:
    """Nudge a price by a small random percentage, preserving its scale."""
    jitter = Decimal(str(round(random.uniform(-0.002, 0.002), 6)))
    return (base_price * (Decimal(1) + jitter)).quantize(base_price)


class BurstRequest(BaseModel):
    """Optional body for POST /burst; ``same_account`` switches to trade mode."""

    same_account: bool = False


def create_app(settings: ProducerSettings) -> FastAPI:
    """Build the control-server FastAPI app wired to shared poll + trade loops."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        timeout = settings.request_timeout_seconds
        async with (
            httpx.AsyncClient(timeout=timeout) as price_http,
            httpx.AsyncClient(timeout=timeout) as platform_http,
        ):
            source = PriceSource(settings.price_api_url, price_http)
            platform = PlatformClient(
                settings.platform_api_url, settings.platform_api_key, platform_http
            )
            tracker = PriceTracker(threshold_pct=Decimal(str(settings.alert_threshold_pct)))
            trades = TradeGenerator(accounts=tuple(settings.account_list))
            app.state.source = source
            app.state.platform = platform
            app.state.tracker = tracker
            app.state.trades = trades
            tasks = [
                asyncio.create_task(
                    _poll_loop(
                        source=source,
                        platform=platform,
                        tracker=tracker,
                        symbols=settings.symbol_list,
                        interval=settings.poll_interval_seconds,
                    )
                ),
                asyncio.create_task(
                    _trade_loop(
                        platform=platform,
                        tracker=tracker,
                        trades=trades,
                        interval=settings.trade_interval_seconds,
                    )
                ),
            ]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="hookit-producer", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/burst")
    async def burst(body: BurstRequest | None = None) -> dict[str, int]:
        """Fire a rapid batch of events; returns how many were published.

        Default: tick events (a traffic spike). With ``same_account=true``:
        concurrent ``trade.executed`` events against one account (the ledger
        demo's lost-update scenario).
        """
        if body is not None and body.same_account:
            published = await _fire_trade_burst(
                platform=app.state.platform,
                tracker=app.state.tracker,
                trades=app.state.trades,
                count=settings.trade_burst_count,
            )
        else:
            published = await _fire_burst(
                platform=app.state.platform,
                tracker=app.state.tracker,
                source=app.state.source,
                symbols=settings.symbol_list,
                count=settings.burst_count,
            )
        return {"published": published}

    @app.post("/duplicate")
    async def duplicate() -> dict[str, Any]:
        """Fire the same payload twice concurrently with one Idempotency-Key."""
        return await _fire_duplicate(
            platform=app.state.platform,
            tracker=app.state.tracker,
            source=app.state.source,
            symbols=settings.symbol_list,
        )

    return app


def main() -> None:
    settings = get_producer_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="level=%(levelname)s logger=%(name)s %(message)s",
    )
    app = create_app(settings)
    uvicorn.run(app, host=settings.control_host, port=settings.control_port)


if __name__ == "__main__":
    main()
