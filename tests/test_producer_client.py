"""Tests for the producer's HTTP clients using an in-memory transport.

No real network: ``httpx.MockTransport`` answers requests so we can assert the
exact ``POST /events`` shape/auth and the spot-price parsing without a server.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
from producer.client import PlatformClient, PriceSource


async def test_price_source_fetches_and_parses_spot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/prices/BTC-USD/spot"
        return httpx.Response(200, json={"data": {"amount": "42000.00", "base": "BTC"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        source = PriceSource("https://api.coinbase.com/v2/prices", http)
        assert await source.spot("BTC-USD") == Decimal("42000.00")


async def test_platform_client_posts_event_with_bearer_auth() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={"event_id": "x", "queued_deliveries": 2})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PlatformClient("http://localhost:8000", "whk_secret", http)
        status = await client.publish("price.tick", {"symbol": "BTC-USD"})

    assert status == 201
    assert seen["path"] == "/events"
    assert seen["auth"] == "Bearer whk_secret"
    assert seen["json"] == {"type": "price.tick", "payload": {"symbol": "BTC-USD"}}


async def test_publish_with_key_sends_idempotency_header_and_returns_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("idempotency-key")
        return httpx.Response(201, json={"event_id": "e-1", "queued_deliveries": 1})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PlatformClient("http://localhost:8000", "k", http)
        status, body = await client.publish_with_key("price.tick", {}, "dup-abc")

    assert status == 201
    assert body == {"event_id": "e-1", "queued_deliveries": 1}
    assert seen["key"] == "dup-abc"


async def test_publish_without_key_omits_idempotency_header() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["has_key"] = "idempotency-key" in request.headers
        return httpx.Response(201, json={"event_id": "e-1", "queued_deliveries": 0})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PlatformClient("http://localhost:8000", "k", http)
        assert await client.publish("price.tick", {}) == 201

    assert seen["has_key"] is False


async def test_fire_duplicate_races_one_key_and_returns_both_responses() -> None:
    """POST /duplicate fires the same payload twice under one Idempotency-Key.

    The mock platform hands out one event_id per distinct key, so both racing
    responses carrying the same event_id proves a single key was reused across
    both concurrent POSTs — the shape the dashboard asserts on.
    """
    from decimal import Decimal

    from producer.__main__ import _fire_duplicate
    from producer.prices import PriceTracker

    tracker = PriceTracker()
    tracker.observe("BTC-USD", Decimal("50000"))

    event_id_by_key: dict[str, str] = {}
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["idempotency-key"]
        bodies.append(json.loads(request.content))
        event_id = event_id_by_key.setdefault(key, f"e-{len(event_id_by_key) + 1}")
        return httpx.Response(201, json={"event_id": event_id, "queued_deliveries": 1})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        platform = PlatformClient("http://localhost:8000", "k", http)
        source = PriceSource("https://api.coinbase.com/v2/prices", http)  # unused: tracker seeded
        result = await _fire_duplicate(
            platform=platform, tracker=tracker, source=source, symbols=["BTC-USD"]
        )

    assert str(result["idempotency_key"]).startswith("dup-")
    assert len(event_id_by_key) == 1  # both POSTs carried the same single key
    assert bodies[0] == bodies[1]  # and byte-for-byte the same payload
    results = result["results"]
    assert isinstance(results, list) and len(results) == 2
    assert results[0]["event_id"] == results[1]["event_id"] == "e-1"
    assert all(r["status"] == 201 for r in results)


async def test_platform_client_swallows_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PlatformClient("http://localhost:8000", "k", http)
        # A transport failure must not raise — the loop keeps going.
        assert await client.publish("price.tick", {}) == 0
