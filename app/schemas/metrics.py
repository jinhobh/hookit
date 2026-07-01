"""Pydantic schemas for the dashboard metrics summary endpoint."""

from __future__ import annotations

from pydantic import BaseModel


class StatusTotals(BaseModel):
    """Delivery counts broken down by status, plus the grand total."""

    pending: int = 0
    in_flight: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_lettered: int = 0
    all: int = 0


class LatencyPercentiles(BaseModel):
    """Per-attempt wall-clock latency percentiles, in milliseconds."""

    p50: float
    p95: float
    p99: float


class MetricsSummaryResponse(BaseModel):
    """Aggregate delivery health for a single project, powering the dashboard.

    ``success_rate`` and ``latency_ms`` are ``None`` when there is not yet
    enough data to compute them (no terminal deliveries / no timed attempts).
    """

    totals: StatusTotals
    success_rate: float | None
    dlq_depth: int
    attempts_total: int
    latency_ms: LatencyPercentiles | None
    throughput_per_min: float
