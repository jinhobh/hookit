"""Pure price → event logic for the live crypto producer.

Deliberately free of I/O (no HTTP, no clock reads baked into control flow) so it
is trivially unit-testable, mirroring how ``app.services.demo_events`` kept its
generator pure. The network fetch lives in :mod:`producer.client`; this module
only *parses* an already-fetched price payload and *builds* the event bodies the
platform ingests.

Two event types are produced:

- ``price.tick``  — emitted for every observation; carries the latest price and
  its change since the previous tick.
- ``price.alert`` — emitted only when a symbol moves at least ``threshold_pct``
  away from its last alert anchor, so the Discord channel isn't spammed with a
  headline on every tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any

TICK_EVENT = "price.tick"
ALERT_EVENT = "price.alert"

#: The event types the platform's showcase endpoints subscribe to.
PRICE_EVENT_TYPES: tuple[str, ...] = (TICK_EVENT, ALERT_EVENT)

#: Default Coinbase spot pairs. Crypto trades 24/7, so a showcase opened on a
#: weekend still shows live data (unlike equities).
DEFAULT_SYMBOLS: tuple[str, ...] = ("BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD")


def parse_spot_price(payload: dict[str, Any]) -> Decimal:
    """Extract the spot amount from a Coinbase ``/prices/{pair}/spot`` response.

    The response shape is ``{"data": {"amount": "61234.56", "base": "BTC",
    "currency": "USD"}}``. Raises :class:`ValueError` on any shape we don't
    recognise so the caller can skip a bad tick rather than crash the loop.
    """
    try:
        return Decimal(str(payload["data"]["amount"]))
    except (KeyError, TypeError, DecimalException) as exc:
        raise ValueError(f"unrecognized spot price payload: {payload!r}") from exc


def _pct_change(prev: Decimal, curr: Decimal) -> Decimal:
    """Percentage change from *prev* to *curr*; 0 when *prev* is 0."""
    if prev == 0:
        return Decimal(0)
    return (curr - prev) / prev * Decimal(100)


def _split_symbol(symbol: str) -> tuple[str, str]:
    """Split ``"BTC-USD"`` into ``("BTC", "USD")``; quote defaults to USD."""
    base, _, quote = symbol.partition("-")
    return base, (quote or "USD")


def _now_iso(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).isoformat()


def build_tick_event(
    symbol: str,
    price: Decimal,
    prev_price: Decimal | None,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a ``price.tick`` event body for one observation.

    *prev_price* is ``None`` for the very first observation of a symbol, in which
    case change/percentage are reported as zero. Prices are serialised as strings
    to preserve exact decimal values through JSON.
    """
    change = Decimal(0) if prev_price is None else price - prev_price
    pct = Decimal(0) if prev_price is None else _pct_change(prev_price, price)
    direction = "flat" if change == 0 else ("up" if change > 0 else "down")
    base, quote = _split_symbol(symbol)
    return TICK_EVENT, {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "price": str(price),
        "change": str(change),
        "change_pct": f"{pct:.2f}",
        "direction": direction,
        "observed_at": _now_iso(now),
    }


def build_alert_event(
    symbol: str,
    price: Decimal,
    anchor_price: Decimal,
    threshold_pct: Decimal,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a ``price.alert`` event body for a threshold-crossing move."""
    pct = _pct_change(anchor_price, price)
    up = pct > 0
    base, quote = _split_symbol(symbol)
    arrow = "▲" if up else "▼"
    return ALERT_EVENT, {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "price": str(price),
        "anchor_price": str(anchor_price),
        "change_pct": f"{pct:.2f}",
        "direction": "up" if up else "down",
        "threshold_pct": str(threshold_pct),
        "headline": f"{base} {arrow} {abs(pct):.2f}% → {price} {quote}",
        "observed_at": _now_iso(now),
    }


@dataclass
class _SymbolState:
    """Per-symbol running state: last tick price and the current alert anchor."""

    last: Decimal
    anchor: Decimal


@dataclass
class PriceTracker:
    """Stateful helper turning a stream of prices into tick/alert events.

    Feed it observations with :meth:`observe`; it returns the events to publish.
    Every observation yields a ``price.tick``; an additional ``price.alert`` is
    appended when the symbol has moved at least ``threshold_pct`` from its anchor,
    at which point the anchor resets to the current price. Purely in-memory and
    deterministic, so it can be driven through canned sequences in tests.
    """

    threshold_pct: Decimal = Decimal("0.5")
    _states: dict[str, _SymbolState] = field(default_factory=dict)

    def observe(
        self, symbol: str, price: Decimal, now: datetime | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        """Record one price and return the events it produces (tick, maybe alert)."""
        state = self._states.get(symbol)
        prev = state.last if state is not None else None
        events: list[tuple[str, dict[str, Any]]] = [build_tick_event(symbol, price, prev, now=now)]

        if state is None:
            self._states[symbol] = _SymbolState(last=price, anchor=price)
            return events

        if abs(_pct_change(state.anchor, price)) >= self.threshold_pct:
            events.append(
                build_alert_event(symbol, price, state.anchor, self.threshold_pct, now=now)
            )
            state.anchor = price
        state.last = price
        return events

    def latest(self) -> dict[str, Decimal]:
        """Return the last observed price per symbol (for burst replay)."""
        return {symbol: st.last for symbol, st in self._states.items()}
