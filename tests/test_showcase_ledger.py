"""Tests for the two-banks ledger demo (Layer 2 of the showcase).

Three tiers:

- **Pure**: ``parse_trade`` input validation, no database.
- **Service** (savepoint-isolated ``sc_session``): dedupe, stale-timestamp
  guard, last-write-wins behavior — single-session semantics.
- **Concurrency + routes** (real sessions, real commits, cleanup): the
  deterministic lost-update race (two sessions held at a barrier between the
  naive bank's read and write, against real Postgres), the safe bank's
  row-locked counterpart, and the ``/showcase/ledger/*`` routes driven through
  real, HMAC-signed deliveries processed by the production worker code.

All DB tests require live Postgres; skipped automatically when unreachable.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.main import app
from app.models.delivery import Delivery, DeliveryStatus
from app.models.demo import DemoLedgerAccount
from app.models.project import Project
from app.services.event_ingestion import ingest_event
from app.services.showcase import (
    TRADE_EXECUTED,
    ShowcaseHandles,
    resolve_showcase,
    seed_showcase,
)
from app.services.showcase_ledger import (
    STARTING_BALANCE,
    ParsedTrade,
    apply_trade_naive,
    apply_trade_safe,
    parse_trade,
)
from app.worker.delivery_worker import process_delivery
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload


def _settings(**overrides: object) -> Settings:
    """A Settings instance with a unique showcase name and Discord disabled."""
    base = get_settings()
    defaults: dict[str, object] = {
        "showcase_project_name": f"__showcase_test_{uuid.uuid4().hex[:10]}__",
        "showcase_discord_webhook_url": "",
        "showcase_api_key": "",
        "database_url": base.database_url,
        "public_base_url": "http://localhost:8000",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _trade_payload(
    account: str = "alice",
    amount: str = "100.00",
    side: str = "buy",
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "trade_id": uuid.uuid4().hex,
        "account": account,
        "symbol": "BTC-USD",
        "side": side,
        "amount": amount,
        "quantity": "0.002",
        "price": "50000",
        "executed_at": (executed_at or datetime.now(UTC)).isoformat(),
    }


def _trade(
    account: str = "alice",
    amount: str = "100.00",
    side: str = "buy",
    executed_at: datetime | None = None,
) -> ParsedTrade:
    parsed = parse_trade(_trade_payload(account, amount, side, executed_at))
    assert parsed is not None
    return parsed


def _no_pause() -> None:
    """Skip the naive bank's deliberate read→write gap where no race is staged."""


def _balance(session: Session, endpoint_id: uuid.UUID, account: str) -> Decimal:
    return session.execute(
        select(DemoLedgerAccount.balance).where(
            DemoLedgerAccount.endpoint_id == endpoint_id,
            DemoLedgerAccount.account == account,
        )
    ).scalar_one()


# ===========================================================================
# Pure tier: trade parsing
# ===========================================================================


def test_parse_trade_accepts_producer_payload() -> None:
    trade = parse_trade(_trade_payload(amount="52.10", side="sell"))
    assert trade is not None
    assert trade.account == "alice"
    assert trade.delta == Decimal("-52.10")  # sells subtract
    assert "sell" in trade.description and "BTC-USD" in trade.description


@pytest.mark.parametrize(
    "mutation",
    [
        {"account": ""},
        {"account": None},
        {"side": "steal"},
        {"amount": "not-money"},
        {"amount": "-5.00"},
        {"executed_at": "yesterday-ish"},
        {"executed_at": "2026-07-02T10:00:00"},  # naive timestamp: no tz
    ],
)
def test_parse_trade_rejects_malformed(mutation: dict[str, Any]) -> None:
    payload = _trade_payload()
    payload.update(mutation)
    assert parse_trade(payload) is None


def test_parse_trade_rejects_non_trade() -> None:
    assert parse_trade({}) is None
    assert parse_trade({"symbol": "BTC-USD", "price": "5"}) is None


# ===========================================================================
# Service tier: single-session bank semantics (savepoint-isolated)
# ===========================================================================


def test_safe_bank_dedupes_on_event_id(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())
    bank = handles.bank_safe_endpoint_id
    event_id = uuid.uuid4()
    trade = _trade(amount="100.00")

    assert apply_trade_safe(sc_session, bank, event_id, trade) == "applied"
    # The redelivery carries the same stable event_id → no-op, money untouched.
    assert apply_trade_safe(sc_session, bank, event_id, trade) == "duplicate"
    assert _balance(sc_session, bank, "alice") == STARTING_BALANCE + Decimal("100.00")


def test_safe_bank_rejects_stale_status_but_never_loses_money(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())
    bank = handles.bank_safe_endpoint_id
    now = datetime.now(UTC)
    fresh = _trade(amount="10.00", executed_at=now)
    stale = _trade(amount="7.00", side="sell", executed_at=now - timedelta(hours=1))

    apply_trade_safe(sc_session, bank, uuid.uuid4(), fresh)
    apply_trade_safe(sc_session, bank, uuid.uuid4(), stale)  # a late retry, out of order

    row = sc_session.get(DemoLedgerAccount, (bank, "alice"))
    assert row is not None
    # Balance applies both (addition commutes); status keeps the fresher trade.
    assert row.balance == STARTING_BALANCE + Decimal("10.00") - Decimal("7.00")
    assert row.status == fresh.description
    assert row.status_as_of == fresh.executed_at


def test_naive_bank_stamps_status_by_arrival_order(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())
    bank = handles.bank_naive_endpoint_id
    now = datetime.now(UTC)
    fresh = _trade(amount="10.00", executed_at=now)
    stale = _trade(amount="7.00", side="sell", executed_at=now - timedelta(hours=1))

    apply_trade_naive(sc_session, bank, fresh, pause=_no_pause)
    apply_trade_naive(sc_session, bank, stale, pause=_no_pause)  # arrives last, wins

    row = sc_session.get(DemoLedgerAccount, (bank, "alice"))
    assert row is not None
    assert row.status == stale.description  # last write won: the display is stale
    assert row.status_as_of == stale.executed_at


def test_naive_bank_reapplies_duplicates(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())
    bank = handles.bank_naive_endpoint_id
    trade = _trade(amount="100.00")

    apply_trade_naive(sc_session, bank, trade, pause=_no_pause)
    apply_trade_naive(sc_session, bank, trade, pause=_no_pause)  # same trade, redelivered
    assert _balance(sc_session, bank, "alice") == STARTING_BALANCE + Decimal("200.00")


# ===========================================================================
# Concurrency tier: the deterministic race, against real Postgres
# ===========================================================================


@pytest.fixture()
def ledger_handles(db_engine: Engine) -> Generator[ShowcaseHandles, None, None]:
    """A committed showcase seed for cross-session tests; cascades away after."""
    Base.metadata.create_all(db_engine)
    settings = _settings()
    with Session(db_engine) as session:
        handles = seed_showcase(session, settings)
        session.commit()
    yield handles
    with Session(db_engine) as session:
        session.execute(delete(Project).where(Project.id == handles.project_id))
        session.commit()


def _run_in_threads(*fns: Callable[[], None]) -> None:
    """Run callables in parallel threads, re-raising the first failure."""
    errors: list[BaseException] = []

    def _guard(fn: Callable[[], None]) -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the test below
            errors.append(exc)

    threads = [threading.Thread(target=_guard, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    if errors:
        raise errors[0]


def test_naive_bank_loses_an_update_under_concurrency(
    ledger_handles: ShowcaseHandles, db_engine: Engine
) -> None:
    """Two real sessions held at a barrier between read and write: one trade
    evaporates from the naive bank's balance — the textbook lost update."""
    bank = ledger_handles.bank_naive_endpoint_id
    account = "race"

    # Settle the account row first so the racers race on UPDATE, not the insert.
    with Session(db_engine) as session:
        apply_trade_naive(session, bank, _trade(account, "25.00"), pause=_no_pause)
        session.commit()
    start = STARTING_BALANCE + Decimal("25.00")

    barrier = threading.Barrier(2)

    def hold_at_barrier() -> None:
        # Both writers have read the same balance; release them together.
        barrier.wait(timeout=10)

    def writer(amount: str) -> Callable[[], None]:
        def run() -> None:
            with Session(db_engine) as session:
                apply_trade_naive(session, bank, _trade(account, amount), pause=hold_at_barrier)
                session.commit()

        return run

    _run_in_threads(writer("100.00"), writer("40.00"))

    with Session(db_engine) as session:
        final = _balance(session, bank, account)
    # Both trades were "applied", yet exactly one survives: whichever writer
    # committed last overwrote the other's credit with its own stale read.
    assert final in {start + Decimal("100.00"), start + Decimal("40.00")}
    assert final != start + Decimal("140.00")


def test_safe_bank_loses_nothing_under_concurrency(
    ledger_handles: ShowcaseHandles, db_engine: Engine
) -> None:
    """Same shape of concurrency, but SELECT … FOR UPDATE serializes the
    writers: the balance is exact."""
    bank = ledger_handles.bank_safe_endpoint_id
    account = "race"

    with Session(db_engine) as session:
        apply_trade_safe(session, bank, uuid.uuid4(), _trade(account, "25.00"))
        session.commit()
    start = STARTING_BALANCE + Decimal("25.00")

    barrier = threading.Barrier(2)

    def writer(amount: str) -> Callable[[], None]:
        def run() -> None:
            barrier.wait(timeout=10)  # maximize overlap; the row lock serializes
            with Session(db_engine) as session:
                apply_trade_safe(session, bank, uuid.uuid4(), _trade(account, amount))
                session.commit()

        return run

    _run_in_threads(writer("100.00"), writer("40.00"))

    with Session(db_engine) as session:
        final = _balance(session, bank, account)
    assert final == start + Decimal("140.00")


# ===========================================================================
# Route tier: /showcase/ledger/* driven through real deliveries
# ===========================================================================


def _handles(db_engine: Engine) -> ShowcaseHandles:
    with Session(db_engine) as session:
        handles = resolve_showcase(session)
        assert handles is not None
        return handles


def _process_bank(db_engine: Engine, endpoint_id: uuid.UUID, worker_client: TestClient) -> int:
    """Process this bank's pending deliveries with the real production code."""
    with Session(db_engine) as session:
        deliveries = (
            session.execute(
                select(Delivery)
                .options(selectinload(Delivery.endpoint), selectinload(Delivery.event))
                .where(
                    Delivery.endpoint_id == endpoint_id,
                    Delivery.status == DeliveryStatus.pending,
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        for delivery in deliveries:
            process_delivery(delivery, session, worker_client)
        session.commit()
        return len(deliveries)


def _ingest_trade(db_engine: Engine, project_id: uuid.UUID, payload: dict[str, Any]) -> int:
    with Session(db_engine) as session:
        _, queued = ingest_event(
            session=session,
            project_id=project_id,
            event_type=TRADE_EXECUTED,
            payload=payload,
            idempotency_key=None,
        )
        session.commit()
        return queued


def _bank_json(data: dict[str, Any], bank: str) -> dict[str, Any]:
    return next(b for b in data["banks"] if b["bank"] == bank)


def test_ledger_reports_both_banks_when_empty(isolated_showcase: str) -> None:
    with TestClient(app) as client:
        resp = client.get("/showcase/ledger")
        assert resp.status_code == 200
        data = resp.json()
    assert data["trade_count"] == 0
    assert Decimal(data["starting_balance"]) == STARTING_BALANCE
    assert [b["bank"] for b in data["banks"]] == ["naive", "safe"]
    for bank in data["banks"]:
        assert bank["mode"] == "healthy"
        assert bank["reconciled"] is True
        assert bank["accounts"] == []


def test_bank_routes_404_for_unknown_or_mismatched_endpoint(
    isolated_showcase: str, db_engine: Engine
) -> None:
    with TestClient(app) as client:
        client.get("/showcase/ledger")  # trigger seeding
        handles = _handles(db_engine)
        assert (
            client.post(f"/showcase/ledger/naive/{uuid.uuid4()}", content=b"{}").status_code == 404
        )
        # A real endpoint id on the *wrong* route is rejected by its marker.
        assert (
            client.post(
                f"/showcase/ledger/naive/{handles.bank_safe_endpoint_id}", content=b"{}"
            ).status_code
            == 404
        )


def test_end_to_end_trade_reconciles_both_banks(isolated_showcase: str, db_engine: Engine) -> None:
    """One real ingested trade, delivered by the real worker code with real
    HMAC signatures, lands both banks exactly on the event log's expectation."""
    with TestClient(app) as client:
        client.get("/showcase/ledger")  # trigger seeding
        handles = _handles(db_engine)
        queued = _ingest_trade(db_engine, handles.project_id, _trade_payload(amount="150.00"))
        assert queued == 2  # fan-out reached both banks (and nothing else)

        _process_bank(db_engine, handles.bank_naive_endpoint_id, client)
        _process_bank(db_engine, handles.bank_safe_endpoint_id, client)

        data = client.get("/showcase/ledger").json()

    assert data["trade_count"] == 1
    for bank_name in ("naive", "safe"):
        bank = _bank_json(data, bank_name)
        assert bank["reconciled"] is True, bank
        assert bank["pending_deliveries"] == 0
        account = bank["accounts"][0]
        assert account["account"] == "alice"
        assert Decimal(account["balance"]) == STARTING_BALANCE + Decimal("150.00")
        assert Decimal(account["drift"]) == 0
        # The tail shows the genuinely signed delivery that arrived.
        assert bank["tail"] and bank["tail"][0]["verified"] is True


def test_lost_ack_double_credits_naive_bank_only(isolated_showcase: str, db_engine: Engine) -> None:
    """Flaky banks process the webhook then answer 500 → the platform retries
    → Bank A credits twice, Bank B's processed-events table absorbs it."""
    with TestClient(app) as client:
        client.get("/showcase/ledger")  # trigger seeding
        handles = _handles(db_engine)
        for bank in ("naive", "safe"):
            resp = client.post("/showcase/ledger/health", json={"bank": bank, "mode": "flaky"})
            assert resp.status_code == 200 and resp.json()["mode"] == "flaky"

        _ingest_trade(db_engine, handles.project_id, _trade_payload(amount="100.00"))
        # First delivery: both banks apply, both answer 500 → retry scheduled.
        _process_bank(db_engine, handles.bank_naive_endpoint_id, client)
        _process_bank(db_engine, handles.bank_safe_endpoint_id, client)
        # The retry redelivers the same event_id to both banks.
        _process_bank(db_engine, handles.bank_naive_endpoint_id, client)
        _process_bank(db_engine, handles.bank_safe_endpoint_id, client)

        # Restore acks; the next retry succeeds and the backlog drains.
        for bank in ("naive", "safe"):
            client.post("/showcase/ledger/health", json={"bank": bank, "mode": "healthy"})
        _process_bank(db_engine, handles.bank_naive_endpoint_id, client)
        _process_bank(db_engine, handles.bank_safe_endpoint_id, client)

        data = client.get("/showcase/ledger").json()

    naive = _bank_json(data, "naive")
    safe = _bank_json(data, "safe")
    assert naive["pending_deliveries"] == 0 and safe["pending_deliveries"] == 0
    # Bank A booked the same trade three times (initial + two redeliveries).
    assert Decimal(naive["accounts"][0]["drift"]) == Decimal("200.00")
    assert naive["reconciled"] is False
    # Bank B applied it exactly once.
    assert Decimal(safe["accounts"][0]["drift"]) == 0
    assert safe["reconciled"] is True


def test_forged_trade_hits_naive_bank_and_bounces_off_safe_bank(
    isolated_showcase: str, db_engine: Engine
) -> None:
    with TestClient(app) as client:
        client.get("/showcase/ledger")  # trigger seeding
        handles = _handles(db_engine)
        forged = {
            "event_id": str(uuid.uuid4()),
            "type": TRADE_EXECUTED,
            "payload": _trade_payload(account="mallory", amount="5000.00"),
        }
        naive = client.post(f"/showcase/ledger/naive/{handles.bank_naive_endpoint_id}", json=forged)
        safe = client.post(f"/showcase/ledger/safe/{handles.bank_safe_endpoint_id}", json=forged)
        assert naive.status_code == 200  # applied, no questions asked
        assert safe.status_code == 401  # constant-time HMAC verification

        data = client.get("/showcase/ledger").json()

    naive_bank = _bank_json(data, "naive")
    safe_bank = _bank_json(data, "safe")
    # The forged trade is not in the platform's event log → pure drift for A.
    account = naive_bank["accounts"][0]
    assert account["account"] == "mallory"
    assert Decimal(account["drift"]) == Decimal("5000.00")
    assert naive_bank["reconciled"] is False
    # Bank B never even opened an account for the forger.
    assert safe_bank["accounts"] == []
    assert safe_bank["reconciled"] is True
    # Both tails show the unsigned request and how each bank answered it.
    assert naive_bank["tail"][0]["verified"] is False
    assert naive_bank["tail"][0]["response_status"] == 200
    assert safe_bank["tail"][0]["verified"] is False
    assert safe_bank["tail"][0]["response_status"] == 401


def test_bank_health_route_validates_input(isolated_showcase: str) -> None:
    with TestClient(app) as client:
        assert (
            client.post(
                "/showcase/ledger/health", json={"bank": "gringotts", "mode": "healthy"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/showcase/ledger/health", json={"bank": "naive", "mode": "on-fire"}
            ).status_code
            == 422
        )
