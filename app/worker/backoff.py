"""Pure backoff calculator for delivery retry scheduling."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta


def compute_next_attempt_at(
    attempt_number: int,
    base_seconds: float,
    cap_seconds: float,
) -> datetime:
    """Return the next retry time using exponential backoff with bounded jitter.

    delay = min(base * 2 ** (attempt - 1), cap) + uniform(0, delay * 0.1)
    """
    delay = min(base_seconds * (2 ** (attempt_number - 1)), cap_seconds)
    jitter = random.uniform(0, delay * 0.1)
    return datetime.now(UTC) + timedelta(seconds=delay + jitter)
