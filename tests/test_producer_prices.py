"""Unit tests for the producer's pure price → event logic.

No network and no database: canned payloads and price sequences in, event bodies
out. Mirrors how ``test_simulate`` unit-tested the old demo event generator.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from producer.prices import (
    ALERT_EVENT,
    TICK_EVENT,
    PriceTracker,
    build_alert_event,
    build_tick_event,
    parse_spot_price,
)


def test_parse_spot_price_reads_coinbase_shape() -> None:
    payload = {"data": {"amount": "61234.56", "base": "BTC", "currency": "USD"}}
    assert parse_spot_price(payload) == Decimal("61234.56")


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": {"amount": "abc"}}, {"data": 1}])
def test_parse_spot_price_rejects_bad_shapes(payload: object) -> None:
    with pytest.raises(ValueError):
        parse_spot_price(payload)  # type: ignore[arg-type]


def test_build_tick_event_first_observation_is_flat() -> None:
    etype, payload = build_tick_event("BTC-USD", Decimal("100"), None)
    assert etype == TICK_EVENT
    assert payload["symbol"] == "BTC-USD"
    assert payload["base"] == "BTC"
    assert payload["quote"] == "USD"
    assert payload["price"] == "100"
    assert payload["change"] == "0"
    assert payload["change_pct"] == "0.00"
    assert payload["direction"] == "flat"
    assert "observed_at" in payload


def test_build_tick_event_reports_up_and_down() -> None:
    _, up = build_tick_event("ETH-USD", Decimal("110"), Decimal("100"))
    assert up["direction"] == "up"
    assert up["change"] == "10"
    assert up["change_pct"] == "10.00"

    _, down = build_tick_event("ETH-USD", Decimal("90"), Decimal("100"))
    assert down["direction"] == "down"
    assert down["change_pct"] == "-10.00"


def test_symbol_without_quote_defaults_usd() -> None:
    _, payload = build_tick_event("BTC", Decimal("1"), None)
    assert payload["base"] == "BTC"
    assert payload["quote"] == "USD"


def test_build_alert_event_shape() -> None:
    etype, payload = build_alert_event("SOL-USD", Decimal("102"), Decimal("100"), Decimal("0.5"))
    assert etype == ALERT_EVENT
    assert payload["direction"] == "up"
    assert payload["change_pct"] == "2.00"
    assert payload["anchor_price"] == "100"
    assert "SOL" in payload["headline"]
    assert "▲" in payload["headline"]


def test_tracker_first_tick_no_alert() -> None:
    tracker = PriceTracker(threshold_pct=Decimal("0.5"))
    events = tracker.observe("BTC-USD", Decimal("100"))
    assert [e[0] for e in events] == [TICK_EVENT]


def test_tracker_emits_alert_only_past_threshold() -> None:
    tracker = PriceTracker(threshold_pct=Decimal("1"))
    tracker.observe("BTC-USD", Decimal("100"))  # anchor = 100

    # +0.5% — below threshold, tick only.
    small = tracker.observe("BTC-USD", Decimal("100.5"))
    assert [e[0] for e in small] == [TICK_EVENT]

    # Now +1.5% from anchor (100) — alert fires and anchor resets to 101.5.
    big = tracker.observe("BTC-USD", Decimal("101.5"))
    assert [e[0] for e in big] == [TICK_EVENT, ALERT_EVENT]
    assert big[1][1]["anchor_price"] == "100"

    # Immediately after reset, a small move produces no new alert.
    after = tracker.observe("BTC-USD", Decimal("101.6"))
    assert [e[0] for e in after] == [TICK_EVENT]


def test_tracker_latest_tracks_last_price_per_symbol() -> None:
    tracker = PriceTracker()
    tracker.observe("BTC-USD", Decimal("100"))
    tracker.observe("ETH-USD", Decimal("50"))
    tracker.observe("BTC-USD", Decimal("101"))
    assert tracker.latest() == {"BTC-USD": Decimal("101"), "ETH-USD": Decimal("50")}
