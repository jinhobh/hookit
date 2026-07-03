"""Tests for the producer's pure trade-event logic (no I/O)."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal

from producer.trades import (
    DEFAULT_ACCOUNTS,
    TRADE_EVENT,
    TradeGenerator,
    build_trade_event,
)


def test_build_trade_event_fields() -> None:
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
    event_type, payload = build_trade_event(
        account="alice",
        symbol="BTC-USD",
        price=Decimal("50000"),
        side="buy",
        quantity=Decimal("0.002"),
        now=now,
    )
    assert event_type == TRADE_EVENT
    assert payload["account"] == "alice"
    assert payload["symbol"] == "BTC-USD"
    assert payload["side"] == "buy"
    # amount = quantity × price, exact decimal string with cents precision.
    assert payload["amount"] == "100.00"
    assert payload["price"] == "50000"
    assert payload["executed_at"] == now.isoformat()
    assert len(payload["trade_id"]) == 32  # uuid4 hex


def test_trade_ids_are_unique() -> None:
    _, a = build_trade_event(
        account="a", symbol="S", price=Decimal(1), side="buy", quantity=Decimal(1)
    )
    _, b = build_trade_event(
        account="a", symbol="S", price=Decimal(1), side="buy", quantity=Decimal(1)
    )
    assert a["trade_id"] != b["trade_id"]


def test_generator_side_follows_the_market() -> None:
    gen = TradeGenerator(accounts=("alice",), rng=random.Random(7))
    gen.next_trade("BTC-USD", Decimal("100"))  # first observation: side random
    _, up = gen.next_trade("BTC-USD", Decimal("110"))
    _, down = gen.next_trade("BTC-USD", Decimal("90"))
    assert up["side"] == "buy"  # buys into strength
    assert down["side"] == "sell"  # sells into weakness


def test_generator_amounts_are_positive_decimal_strings() -> None:
    gen = TradeGenerator(rng=random.Random(42))
    for _ in range(20):
        _, payload = gen.next_trade("ETH-USD", Decimal("2500"))
        amount = Decimal(payload["amount"])
        assert amount > 0
        assert payload["account"] in DEFAULT_ACCOUNTS
        # cents precision, ready for exact ledger arithmetic
        assert amount == amount.quantize(Decimal("0.01"))


def test_burst_same_account_hits_exactly_one_account() -> None:
    gen = TradeGenerator(rng=random.Random(1))
    events = gen.burst_same_account("SOL-USD", Decimal("150"), count=8)
    assert len(events) == 8
    accounts = {payload["account"] for _, payload in events}
    assert len(accounts) == 1  # the whole storm lands on one account
    assert all(etype == TRADE_EVENT for etype, _ in events)
    # Distinct trades (unique ids), same direction, varying sizes.
    assert len({p["trade_id"] for _, p in events}) == 8
    assert len({p["side"] for _, p in events}) == 1
