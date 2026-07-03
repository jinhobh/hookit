"""The two-banks ledger demo: receiver-side correctness, made visible.

A webhook platform cannot stop two writers from corrupting a downstream
ledger — it sits upstream of that. What it provides is **at-least-once
delivery, a stable event id, a signed timestamp, and an attempt counter**;
exactly the primitives a receiver needs to stay correct under duplicates,
concurrency, disorder, and forgery. This module implements a live victim to
prove it: two "bank" receivers fed the same ``trade.executed`` stream through
the unmodified delivery path.

- :func:`apply_trade_naive` — **Bank A**: read balance → pause → blind write.
  No dedupe, no locking, no timestamp guard. The pause widens the read/write
  window so the lost-update race is reliably visible rather than a coin flip.
- :func:`apply_trade_safe` — **Bank B**: an ``INSERT`` into the
  processed-events table (duplicates conflict → no-op) plus a
  ``SELECT … FOR UPDATE`` balance update in one transaction, and a stale
  ``executed_at`` guard on the last-write-wins status fields.
- :func:`build_ledger_view` — the reconciliation meter: both banks' balances
  diffed against expected balances computed from the platform's own event log
  (the source of truth), drift in dollars.

Money is ``Decimal`` end to end — payload amounts are decimal strings, the
column is ``numeric`` — so drift means a broken receiver, never float noise.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from time import sleep
from typing import Any, Literal

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.delivery import Delivery, DeliveryStatus
from app.models.demo import DemoLedgerAccount, DemoLedgerProcessed, DemoReceiverHealth
from app.services.showcase import TRADE_EXECUTED

# Every account opens with this balance at both banks (and in the expected-
# balance computation), so "drift" is meaningful from the very first trade.
STARTING_BALANCE = Decimal("10000.00")

# How long Bank A deliberately sits between reading and writing the balance,
# so concurrent deliveries interleave dependably instead of by coin flip.
NAIVE_READ_WRITE_GAP_SECONDS = 0.05

_CENTS = Decimal("0.01")


class BankMode(StrEnum):
    """Tri-state bank health: ``flaky`` processes the webhook, then fails."""

    healthy = "healthy"
    flaky = "flaky"
    down = "down"


# ---------------------------------------------------------------------------
# Bank health (mode)
# ---------------------------------------------------------------------------


def get_bank_mode(session: Session, endpoint_id: uuid.UUID) -> BankMode:
    """Return the bank's current mode (defaults healthy)."""
    row = session.get(DemoReceiverHealth, endpoint_id)
    if row is None:
        return BankMode.healthy
    try:
        return BankMode(row.mode)
    except ValueError:
        return BankMode.healthy


def set_bank_mode(session: Session, endpoint_id: uuid.UUID, mode: BankMode) -> None:
    """Upsert the bank's mode. The caller commits."""
    row = session.get(DemoReceiverHealth, endpoint_id)
    if row is None:
        session.add(DemoReceiverHealth(endpoint_id=endpoint_id, healthy=True, mode=mode.value))
    else:
        row.mode = mode.value
    session.flush()


# ---------------------------------------------------------------------------
# Trade parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedTrade:
    """The fields of a ``trade.executed`` payload a bank actually uses."""

    account: str
    delta: Decimal  # signed: buy adds, sell subtracts
    executed_at: datetime
    description: str  # e.g. "buy $532.10 BTC-USD"


def parse_trade(payload: dict[str, Any]) -> ParsedTrade | None:
    """Parse a trade payload; None when it does not look like a trade at all."""
    account = payload.get("account")
    side = payload.get("side")
    amount_raw = payload.get("amount")
    executed_raw = payload.get("executed_at")
    if not isinstance(account, str) or not account or side not in ("buy", "sell"):
        return None
    try:
        amount = Decimal(str(amount_raw)).quantize(_CENTS)
        executed_at = datetime.fromisoformat(str(executed_raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if amount < 0 or executed_at.tzinfo is None:
        return None
    symbol = payload.get("symbol", "")
    return ParsedTrade(
        account=account,
        delta=amount if side == "buy" else -amount,
        executed_at=executed_at,
        description=f"{side} ${amount} {symbol}".strip(),
    )


# ---------------------------------------------------------------------------
# Bank A — naive apply (deliberately wrong, in the ways real receivers are)
# ---------------------------------------------------------------------------


def _ensure_account_row(session: Session, endpoint_id: uuid.UUID, account: str) -> None:
    """Create the account row at STARTING_BALANCE if it does not exist yet."""
    session.execute(
        pg_insert(DemoLedgerAccount)
        .values(endpoint_id=endpoint_id, account=account, balance=STARTING_BALANCE)
        .on_conflict_do_nothing(index_elements=["endpoint_id", "account"])
    )


def apply_trade_naive(
    session: Session,
    endpoint_id: uuid.UUID,
    trade: ParsedTrade,
    *,
    pause: Callable[[], None] | None = None,
) -> None:
    """Apply a trade the way a naive receiver does. The caller commits.

    Read the balance without a lock, wait, then write ``read + delta`` as a
    blind value — the textbook lost update when two deliveries interleave.
    The status fields are stamped in arrival order (last write wins), which
    the time-travel scenario exploits with late retries. No dedupe: a
    redelivered event is credited again. *pause* is injectable so the race
    test can hold both writers at a barrier between read and write.
    """
    _ensure_account_row(session, endpoint_id, account=trade.account)
    read_balance = session.execute(
        select(DemoLedgerAccount.balance).where(
            DemoLedgerAccount.endpoint_id == endpoint_id,
            DemoLedgerAccount.account == trade.account,
        )
    ).scalar_one()

    if pause is not None:
        pause()
    else:
        sleep(NAIVE_READ_WRITE_GAP_SECONDS)

    session.execute(
        update(DemoLedgerAccount)
        .where(
            DemoLedgerAccount.endpoint_id == endpoint_id,
            DemoLedgerAccount.account == trade.account,
        )
        .values(
            balance=read_balance + trade.delta,  # stale read → lost update
            status=trade.description,
            status_as_of=trade.executed_at,
        )
    )


# ---------------------------------------------------------------------------
# Bank B — safe apply (dedupe + row lock + stale-timestamp guard)
# ---------------------------------------------------------------------------


def apply_trade_safe(
    session: Session,
    endpoint_id: uuid.UUID,
    event_id: uuid.UUID,
    trade: ParsedTrade,
) -> Literal["applied", "duplicate"]:
    """Apply a trade exactly once, atomically. The caller commits.

    All in one transaction: record the platform's stable ``event_id`` in the
    processed-events table (a conflicting INSERT means this delivery is a
    retry of already-applied work → answer without touching money), then
    update the balance under ``SELECT … FOR UPDATE`` so concurrent deliveries
    serialize instead of interleaving. The last-write-wins status fields only
    move forward in event time (``executed_at``), so late retries can never
    overwrite fresher state.
    """
    inserted = session.execute(
        pg_insert(DemoLedgerProcessed)
        .values(endpoint_id=endpoint_id, event_id=event_id)
        .on_conflict_do_nothing(index_elements=["endpoint_id", "event_id"])
        .returning(DemoLedgerProcessed.event_id)
    ).first()
    if inserted is None:
        return "duplicate"

    _ensure_account_row(session, endpoint_id, account=trade.account)
    row = session.execute(
        select(DemoLedgerAccount)
        .where(
            DemoLedgerAccount.endpoint_id == endpoint_id,
            DemoLedgerAccount.account == trade.account,
        )
        .with_for_update()
    ).scalar_one()

    row.balance = row.balance + trade.delta
    if row.status_as_of is None or trade.executed_at > row.status_as_of:
        row.status = trade.description
        row.status_as_of = trade.executed_at
    session.flush()
    return "applied"


# ---------------------------------------------------------------------------
# Reconciliation — diff both banks against the platform's own event log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedAccount:
    """What one account should hold, per the durable event log."""

    delta: Decimal
    trades: int
    latest_executed_at: datetime | None


@dataclass(frozen=True)
class AccountView:
    """One account at one bank, diffed against the event log."""

    account: str
    balance: Decimal
    expected: Decimal
    drift: Decimal
    status: str | None
    status_as_of: datetime | None
    status_stale: bool


@dataclass(frozen=True)
class BankView:
    """One bank's full reconciliation state."""

    endpoint_id: uuid.UUID
    mode: BankMode
    accounts: list[AccountView]
    total_drift: Decimal
    pending_deliveries: int
    dead_lettered_deliveries: int


def expected_from_event_log(session: Session, project_id: uuid.UUID) -> dict[str, ExpectedAccount]:
    """Aggregate the project's ``trade.executed`` events into expected state.

    The event log is the source of truth: whatever the platform durably
    ingested is what a correct consumer must end up reflecting. Forged
    requests POSTed straight at a bank never appear here — which is exactly
    why the forger scenario shows up as drift.
    """
    rows = session.execute(
        text(
            """
            SELECT payload->>'account' AS account,
                   SUM(CASE WHEN payload->>'side' = 'sell'
                            THEN -(payload->>'amount')::numeric
                            ELSE (payload->>'amount')::numeric END) AS delta,
                   COUNT(*) AS trades,
                   MAX(payload->>'executed_at') AS latest_executed_at
            FROM events
            WHERE project_id = :project_id
              AND type = :trade_type
              AND payload->>'account' IS NOT NULL
              AND payload->>'amount' ~ '^[0-9]+(\\.[0-9]+)?$'
            GROUP BY 1
            """
        ),
        {"project_id": project_id, "trade_type": TRADE_EXECUTED},
    ).all()

    expected: dict[str, ExpectedAccount] = {}
    for account, delta, trades, latest_raw in rows:
        latest: datetime | None
        try:
            latest = datetime.fromisoformat(latest_raw) if latest_raw else None
        except ValueError:
            latest = None
        expected[account] = ExpectedAccount(
            delta=Decimal(delta).quantize(_CENTS),
            trades=int(trades),
            latest_executed_at=latest,
        )
    return expected


def _delivery_counts(session: Session, endpoint_id: uuid.UUID) -> tuple[int, int]:
    """Return (pending or in-flight, dead-lettered) delivery counts for a bank."""
    rows: dict[DeliveryStatus, int] = {}
    for status, count in session.execute(
        select(Delivery.status, func.count())
        .where(Delivery.endpoint_id == endpoint_id)
        .group_by(Delivery.status)
    ).tuples():
        rows[status] = count
    in_progress = rows.get(DeliveryStatus.pending, 0) + rows.get(DeliveryStatus.in_flight, 0)
    return in_progress, rows.get(DeliveryStatus.dead_lettered, 0)


def build_bank_view(
    session: Session,
    endpoint_id: uuid.UUID,
    expected: dict[str, ExpectedAccount],
) -> BankView:
    """Assemble one bank's accounts, drift, and delivery backlog."""
    ledger_rows = {
        row.account: row
        for row in session.execute(
            select(DemoLedgerAccount).where(DemoLedgerAccount.endpoint_id == endpoint_id)
        ).scalars()
    }
    accounts: list[AccountView] = []
    total_drift = Decimal("0")
    for account in sorted(set(expected) | set(ledger_rows)):
        row = ledger_rows.get(account)
        exp = expected.get(account)
        balance = row.balance if row is not None else STARTING_BALANCE
        expected_balance = STARTING_BALANCE + (exp.delta if exp is not None else Decimal("0"))
        drift = (balance - expected_balance).quantize(_CENTS)
        status_as_of = row.status_as_of if row is not None else None
        latest = exp.latest_executed_at if exp is not None else None
        accounts.append(
            AccountView(
                account=account,
                balance=balance,
                expected=expected_balance,
                drift=drift,
                status=row.status if row is not None else None,
                status_as_of=status_as_of,
                status_stale=(
                    status_as_of is not None and latest is not None and status_as_of < latest
                ),
            )
        )
        total_drift += abs(drift)

    pending, dead = _delivery_counts(session, endpoint_id)
    return BankView(
        endpoint_id=endpoint_id,
        mode=get_bank_mode(session, endpoint_id),
        accounts=accounts,
        total_drift=total_drift,
        pending_deliveries=pending,
        dead_lettered_deliveries=dead,
    )
