"""Public router for the two-banks ledger demo (Layer 2 of the showcase).

Same trust model as ``app.routers.showcase``: everything is unauthenticated
but hard-scoped to the single seeded showcase project, and the bank receivers
are pure sinks — they can only accept a request and answer, never initiate
one, and only ever resolve endpoints tagged with the reserved bank markers.

- ``GET  /showcase/ledger``          — both banks' balances diffed against the
  expected balances computed from the platform's own event log (drift in
  dollars), plus each bank's mode, delivery backlog, and received-request tail.
- ``POST /showcase/ledger/health``   — set one bank healthy / flaky / down.
- ``POST /showcase/ledger/naive/{endpoint_id}`` — **Bank A**: applies every
  webhook as it arrives; no signature check, no dedupe, no locking.
- ``POST /showcase/ledger/safe/{endpoint_id}``  — **Bank B**: HMAC
  verification, processed-events dedupe, row-locked atomic apply, and a
  stale-timestamp guard.

The bank receiver handlers are deliberately synchronous (``def``): they run in
the threadpool, so concurrent deliveries from concurrent worker loops really
do overlap inside Bank A's read → pause → write window.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models.endpoint import Endpoint
from app.routers.showcase import get_showcase
from app.schemas.showcase import (
    LedgerAccountItem,
    LedgerBankItem,
    LedgerHealthRequest,
    LedgerHealthResponse,
    LedgerResponse,
    ReceivedRequestItem,
)
from app.services.crypto import decrypt_secret
from app.services.showcase import (
    BANK_NAIVE_MARKER,
    BANK_SAFE_MARKER,
    ShowcaseHandles,
    list_inbox,
    record_received_request,
)
from app.services.showcase_ledger import (
    STARTING_BALANCE,
    BankMode,
    BankView,
    apply_trade_naive,
    apply_trade_safe,
    build_bank_view,
    expected_from_event_log,
    get_bank_mode,
    parse_trade,
    set_bank_mode,
)
from app.worker.signing import verify_signature

router = APIRouter(prefix="/showcase/ledger", tags=["showcase"])

# How many recent received requests each bank's tail shows on the dashboard.
_TAIL_LIMIT = 8


async def _raw_body(request: Request) -> bytes:
    """Async dependency feeding the raw body into the sync bank handlers."""
    return await request.body()


def _bank_endpoint(session: Session, endpoint_id: uuid.UUID, marker: str) -> Endpoint:
    """Resolve a bank endpoint by id **and** its reserved marker, or 404."""
    endpoint = session.execute(
        select(Endpoint).where(
            Endpoint.id == endpoint_id,
            Endpoint.event_types.contains([marker]),
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Unknown showcase bank endpoint")
    return endpoint


def _parse_envelope(body: bytes) -> tuple[uuid.UUID | None, str, dict[str, Any]]:
    """Split a delivery body into (event_id, event_type, payload), tolerantly."""
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        doc = None
    if not isinstance(doc, dict):
        return None, "unknown", {}
    event_type = str(doc.get("type", "unknown"))
    payload = doc.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    try:
        event_id = uuid.UUID(str(doc.get("event_id")))
    except ValueError:
        event_id = None
    return event_id, event_type, payload


def _attempt_number(request: Request) -> int:
    try:
        return int(request.headers.get("x-webhook-attempt", "1"))
    except ValueError:
        return 1


def _record(
    session: Session,
    request: Request,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    verified: bool,
    response_status: int,
    body: bytes,
) -> None:
    """Append this request to the bank's tail and commit everything pending.

    One commit covers both the ledger mutation and the tail row, so a bank's
    state and its visible history can never disagree.
    """
    record_received_request(
        session,
        endpoint_id=endpoint_id,
        event_type=event_type,
        attempt=_attempt_number(request),
        verified=verified,
        response_status=response_status,
        signature_header=request.headers.get("x-webhook-signature"),
        timestamp_header=request.headers.get("x-webhook-timestamp"),
        body=body.decode("utf-8", errors="replace"),
    )
    session.commit()


@router.post("/naive/{endpoint_id}")
def bank_naive_receiver(
    endpoint_id: uuid.UUID,
    request: Request,
    body: bytes = Depends(_raw_body),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Bank A: applies whatever arrives, in arrival order, with no checks.

    The signature is verified for *display only* (the tail shows what really
    arrived) — Bank A ignores the result, which is exactly its vulnerability
    to the forger. Flaky mode processes the trade and then answers 500, so the
    platform correctly retries and Bank A credits the same trade twice.
    """
    endpoint = _bank_endpoint(session, endpoint_id, BANK_NAIVE_MARKER)
    _, event_type, payload = _parse_envelope(body)
    verified, _ = verify_signature(
        decrypt_secret(endpoint.secret_enc), request.headers.get("x-webhook-signature"), body
    )
    mode = get_bank_mode(session, endpoint_id)

    if mode == BankMode.down:
        _record(
            session,
            request,
            endpoint_id=endpoint_id,
            event_type=event_type,
            verified=verified,
            response_status=503,
            body=body,
        )
        return JSONResponse({"status": "bank unavailable"}, status_code=503)

    trade = parse_trade(payload)
    if trade is not None:
        apply_trade_naive(session, endpoint_id, trade)

    response_status = 200 if mode == BankMode.healthy else 500
    _record(
        session,
        request,
        endpoint_id=endpoint_id,
        event_type=event_type,
        verified=verified,
        response_status=response_status,
        body=body,
    )
    if mode == BankMode.flaky:
        # The trade *was* applied and committed — the ack is what got lost.
        return JSONResponse({"status": "internal error"}, status_code=500)
    return JSONResponse({"status": "applied" if trade is not None else "ignored"})


@router.post("/safe/{endpoint_id}")
def bank_safe_receiver(
    endpoint_id: uuid.UUID,
    request: Request,
    body: bytes = Depends(_raw_body),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Bank B: verify, dedupe on the stable event id, apply atomically."""
    endpoint = _bank_endpoint(session, endpoint_id, BANK_SAFE_MARKER)
    event_id, event_type, payload = _parse_envelope(body)
    verified, reason = verify_signature(
        decrypt_secret(endpoint.secret_enc), request.headers.get("x-webhook-signature"), body
    )
    mode = get_bank_mode(session, endpoint_id)

    if mode == BankMode.down:
        _record(
            session,
            request,
            endpoint_id=endpoint_id,
            event_type=event_type,
            verified=verified,
            response_status=503,
            body=body,
        )
        return JSONResponse({"status": "bank unavailable"}, status_code=503)

    if not verified:
        _record(
            session,
            request,
            endpoint_id=endpoint_id,
            event_type=event_type,
            verified=False,
            response_status=401,
            body=body,
        )
        return JSONResponse({"error": reason}, status_code=401)

    trade = parse_trade(payload)
    if event_id is None or trade is None:
        _record(
            session,
            request,
            endpoint_id=endpoint_id,
            event_type=event_type,
            verified=True,
            response_status=400,
            body=body,
        )
        return JSONResponse({"error": "not a recognizable trade delivery"}, status_code=400)

    result = apply_trade_safe(session, endpoint_id, event_id, trade)

    response_status = 200 if mode == BankMode.healthy else 500
    _record(
        session,
        request,
        endpoint_id=endpoint_id,
        event_type=event_type,
        verified=True,
        response_status=response_status,
        body=body,
    )
    if mode == BankMode.flaky:
        # Processed (or deduped) and committed; only the ack is withheld.
        return JSONResponse({"status": "internal error"}, status_code=500)
    return JSONResponse({"status": result})


@router.get("", response_model=LedgerResponse)
def ledger(
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
) -> LedgerResponse:
    """The reconciliation meter: both banks diffed against the event log.

    ``drift`` is exact decimal dollars. A bank with nonzero drift and nothing
    left in flight has genuinely lost or invented money; while deliveries are
    still pending/retrying the dashboard shows it as syncing instead.
    """
    expected = expected_from_event_log(session, handles.project_id)

    def _bank_item(bank: str, view: BankView) -> LedgerBankItem:
        return LedgerBankItem(
            bank=bank,
            endpoint_id=view.endpoint_id,
            mode=view.mode.value,
            total_drift=str(view.total_drift),
            reconciled=view.total_drift == 0,
            pending_deliveries=view.pending_deliveries,
            dead_lettered_deliveries=view.dead_lettered_deliveries,
            accounts=[
                LedgerAccountItem(
                    account=a.account,
                    balance=str(a.balance),
                    expected=str(a.expected),
                    drift=str(a.drift),
                    reconciled=a.drift == 0,
                    status=a.status,
                    status_as_of=a.status_as_of,
                    status_stale=a.status_stale,
                )
                for a in view.accounts
            ],
            tail=[
                ReceivedRequestItem.model_validate(r)
                for r in list_inbox(session, view.endpoint_id, limit=_TAIL_LIMIT)
            ],
        )

    naive_view = build_bank_view(session, handles.bank_naive_endpoint_id, expected)
    safe_view = build_bank_view(session, handles.bank_safe_endpoint_id, expected)
    return LedgerResponse(
        server_time=datetime.now(UTC),
        starting_balance=str(STARTING_BALANCE),
        trade_count=sum(e.trades for e in expected.values()),
        banks=[_bank_item("naive", naive_view), _bank_item("safe", safe_view)],
    )


@router.post("/health", response_model=LedgerHealthResponse)
def set_bank_health(
    body: LedgerHealthRequest,
    handles: ShowcaseHandles = Depends(get_showcase),
    session: Session = Depends(get_session),
) -> LedgerHealthResponse:
    """Set one bank healthy / flaky / down (the lost-ack and time-travel knobs)."""
    endpoint_id = (
        handles.bank_naive_endpoint_id if body.bank == "naive" else handles.bank_safe_endpoint_id
    )
    set_bank_mode(session, endpoint_id, BankMode(body.mode))
    session.commit()
    return LedgerHealthResponse(bank=body.bank, endpoint_id=endpoint_id, mode=body.mode)
