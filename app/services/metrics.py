"""Prometheus metrics singletons for the webhook delivery platform."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

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
