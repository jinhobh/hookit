"""Prometheus metrics singletons plus dashboard aggregation queries.

The Prometheus counters below are process-local and reset on restart. The
``delivery_summary`` function computes durable, per-project rollups directly
from the ``deliveries`` / ``delivery_attempts`` tables — this is what the
dashboard reads, so the numbers survive worker restarts and are scoped to the
authenticated project.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from prometheus_client import Counter, Histogram
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.delivery import Delivery, DeliveryStatus
from app.models.delivery_attempt import DeliveryAttempt
from app.models.endpoint import Endpoint
from app.schemas.metrics import LatencyPercentiles, MetricsSummaryResponse, StatusTotals

DELIVERIES_TOTAL: Counter = Counter(
    "webhook_deliveries",
    "Total delivery attempts by outcome.",
    ["outcome"],
)

DELIVERY_DURATION_SECONDS: Histogram = Histogram(
    "webhook_delivery_attempt_duration_seconds",
    "Wall-clock duration of each delivery attempt in seconds.",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

DELIVERIES_CLAIMED_TOTAL: Counter = Counter(
    "webhook_deliveries_claimed",
    "Total deliveries claimed by the worker.",
)

# Deliveries succeeding within this window define the "per minute" throughput.
_THROUGHPUT_WINDOW_SECONDS = 60


def delivery_summary(session: Session, project_id: uuid.UUID) -> MetricsSummaryResponse:
    """Compute aggregate delivery health for one project.

    Returns status counts, terminal success rate, dead-letter depth, total
    attempts (retries included), per-attempt latency percentiles, and a
    trailing one-minute succeeded-delivery throughput. All figures are scoped
    to *project_id* via the deliveries → endpoints join.
    """
    status_rows = (
        session.execute(
            select(Delivery.status, func.count())
            .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
            .where(Endpoint.project_id == project_id)
            .group_by(Delivery.status)
        )
        .tuples()
        .all()
    )
    counts: dict[DeliveryStatus, int] = dict(status_rows)

    totals = StatusTotals(
        pending=counts.get(DeliveryStatus.pending, 0),
        in_flight=counts.get(DeliveryStatus.in_flight, 0),
        succeeded=counts.get(DeliveryStatus.succeeded, 0),
        failed=counts.get(DeliveryStatus.failed, 0),
        dead_lettered=counts.get(DeliveryStatus.dead_lettered, 0),
        all=sum(counts.values()),
    )

    # Success rate over *terminal* deliveries only (succeeded vs dead-lettered);
    # pending/in-flight/transient-failed are still in progress and excluded.
    terminal = totals.succeeded + totals.dead_lettered
    success_rate = round(totals.succeeded / terminal, 4) if terminal > 0 else None

    attempts_total = session.execute(
        select(func.count())
        .select_from(DeliveryAttempt)
        .join(Delivery, DeliveryAttempt.delivery_id == Delivery.id)
        .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        .where(Endpoint.project_id == project_id)
    ).scalar_one()

    p50, p95, p99 = session.execute(
        select(
            func.percentile_cont(0.5).within_group(DeliveryAttempt.duration_ms.asc()),
            func.percentile_cont(0.95).within_group(DeliveryAttempt.duration_ms.asc()),
            func.percentile_cont(0.99).within_group(DeliveryAttempt.duration_ms.asc()),
        )
        .select_from(DeliveryAttempt)
        .join(Delivery, DeliveryAttempt.delivery_id == Delivery.id)
        .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        .where(Endpoint.project_id == project_id, DeliveryAttempt.duration_ms.is_not(None))
    ).one()
    latency = (
        LatencyPercentiles(p50=float(p50), p95=float(p95), p99=float(p99))
        if p50 is not None
        else None
    )

    cutoff = datetime.now(UTC) - timedelta(seconds=_THROUGHPUT_WINDOW_SECONDS)
    succeeded_recent = session.execute(
        select(func.count())
        .select_from(Delivery)
        .join(Endpoint, Delivery.endpoint_id == Endpoint.id)
        .where(
            Endpoint.project_id == project_id,
            Delivery.status == DeliveryStatus.succeeded,
            Delivery.updated_at >= cutoff,
        )
    ).scalar_one()

    return MetricsSummaryResponse(
        totals=totals,
        success_rate=success_rate,
        dlq_depth=totals.dead_lettered,
        attempts_total=attempts_total,
        latency_ms=latency,
        throughput_per_min=float(succeeded_recent),
    )
