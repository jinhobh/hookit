"""Pure trade → event logic for the live crypto producer (Layer 2 showcase).

Emits ``trade.executed`` events against a small set of demo accounts. Each
trade is a random quantity of the asset priced at the *real* observed spot
price, so the stream stays grounded in live data — nothing is a canned
animation. Like :mod:`producer.prices`, this module is deliberately free of
I/O and takes an injectable :class:`random.Random` plus an optional clock so
tests are deterministic.

The platform fans each ``trade.executed`` out to the two showcase "banks"
(naive vs. correct receivers); the payload therefore carries exactly the
primitives a receiver needs to stay correct under duplicates, concurrency, and
disorder: a unique ``trade_id``, an ``executed_at`` timestamp, and an exact
decimal ``amount``.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

TRADE_EVENT = "trade.executed"

#: Default demo accounts the trade stream is spread across.
DEFAULT_ACCOUNTS: tuple[str, ...] = ("alice", "bob", "carol", "dan")

#: Bounds for one trade's notional dollar amount.
_MIN_NOTIONAL = 25.0
_MAX_NOTIONAL = 1500.0

_CENTS = Decimal("0.01")
_QTY = Decimal("0.00000001")


def _now_iso(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).isoformat()


def build_trade_event(
    *,
    account: str,
    symbol: str,
    price: Decimal,
    side: str,
    quantity: Decimal,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build one ``trade.executed`` event body; ``amount = quantity × price``.

    Money values are serialised as strings to preserve exact decimals through
    JSON — the banks (and the reconciliation meter) parse them back into
    ``Decimal`` / ``numeric``, never floats.
    """
    return TRADE_EVENT, {
        "trade_id": uuid.uuid4().hex,
        "account": account,
        "symbol": symbol,
        "side": side,
        "quantity": str(quantity),
        "amount": str((quantity * price).quantize(_CENTS)),
        "price": str(price),
        "executed_at": _now_iso(now),
    }


@dataclass
class TradeGenerator:
    """Stateful helper turning live prices into a stream of demo trades.

    Tracks the last price it saw per symbol so a trade's ``side`` follows the
    real market direction (buy into strength, sell into weakness); the very
    first observation of a symbol picks a side at random. ``rng`` is
    injectable so tests can seed it.
    """

    accounts: tuple[str, ...] = DEFAULT_ACCOUNTS
    rng: random.Random = field(default_factory=random.Random)
    _last_price: dict[str, Decimal] = field(default_factory=dict)

    def _quantity(self, price: Decimal) -> Decimal:
        """A quantity worth a random notional at the current live price."""
        notional = Decimal(str(round(self.rng.uniform(_MIN_NOTIONAL, _MAX_NOTIONAL), 2)))
        if price <= 0:
            return Decimal(0)
        return (notional / price).quantize(_QTY)

    def _side(self, symbol: str, price: Decimal) -> str:
        prev = self._last_price.get(symbol)
        if prev is None or prev == price:
            return self.rng.choice(("buy", "sell"))
        return "buy" if price > prev else "sell"

    def next_trade(
        self, symbol: str, price: Decimal, now: datetime | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Build the next trade for one live price observation."""
        side = self._side(symbol, price)
        account = self.rng.choice(self.accounts)
        self._last_price[symbol] = price
        return build_trade_event(
            account=account,
            symbol=symbol,
            price=price,
            side=side,
            quantity=self._quantity(price),
            now=now,
        )

    def burst_same_account(
        self, symbol: str, price: Decimal, count: int, now: datetime | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        """Build *count* trades that all hit **one** account.

        Published concurrently, these land on multiple worker loops at once —
        the "two writers, one account" chaos scenario that makes the naive
        bank's read-modify-write lose an update.
        """
        account = self.rng.choice(self.accounts)
        side = self._side(symbol, price)
        self._last_price[symbol] = price
        return [
            build_trade_event(
                account=account,
                symbol=symbol,
                price=price,
                side=side,
                quantity=self._quantity(price),
                now=now,
            )
            for _ in range(count)
        ]
