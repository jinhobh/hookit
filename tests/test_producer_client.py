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


async def test_platform_client_swallows_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PlatformClient("http://localhost:8000", "k", http)
        # A transport failure must not raise — the loop keeps going.
        assert await client.publish("price.tick", {}) == 0
