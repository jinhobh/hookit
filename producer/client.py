"""HTTP clients for the producer: upstream price fetch + platform publish.

Both wrap a shared :class:`httpx.AsyncClient`. Kept thin and typed so the poll
loop in :mod:`producer.__main__` reads as orchestration, and so the publish path
can be exercised against an ``httpx.MockTransport`` in tests without a live
server.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from producer.prices import parse_spot_price

logger = logging.getLogger(__name__)


class PriceSource:
    """Fetches spot prices from the keyless Coinbase v2 prices API."""

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._client = client

    async def spot(self, symbol: str) -> Decimal:
        """Return the current spot price for a product id (e.g. ``"BTC-USD"``).

        Raises ``httpx.HTTPStatusError`` on a non-2xx response and
        :class:`ValueError` on an unrecognised body — the caller skips the tick.
        """
        resp = await self._client.get(f"{self._base}/{symbol}/spot")
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        return parse_spot_price(payload)


class PlatformClient:
    """Publishes events to the platform's real ``POST /events`` ingestion API."""

    def __init__(self, base_url: str, api_key: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._client = client

    async def publish(self, event_type: str, payload: dict[str, Any]) -> int:
        """POST one event; return the HTTP status code.

        Never raises on a non-2xx: a rejected event must not kill the producer
        loop. The caller logs; the reliability engine is the platform's job.
        """
        status, _ = await self.publish_with_key(event_type, payload, idempotency_key=None)
        return status

    async def publish_with_key(
        self, event_type: str, payload: dict[str, Any], idempotency_key: str | None
    ) -> tuple[int, dict[str, Any]]:
        """POST one event, optionally with an ``Idempotency-Key``.

        Returns ``(status_code, response_body_dict)`` — the body carries the
        platform's ``event_id`` / ``queued_deliveries``, which the duplicate
        demo compares across racing requests. Never raises; a transport error
        returns ``(0, {})``.
        """
        headers = {"Authorization": f"Bearer {self._key}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = await self._client.post(
                f"{self._base}/events",
                json={"type": event_type, "payload": payload},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("publish failed for %s: %s", event_type, exc)
            return 0, {}
        if resp.status_code >= 300:
            logger.warning("publish %s returned HTTP %s", event_type, resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body if isinstance(body, dict) else {}
