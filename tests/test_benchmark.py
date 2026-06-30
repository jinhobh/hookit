"""Unit tests for the benchmark harness helpers."""

from __future__ import annotations

import pytest
from benchmark.runner import LatencyStats, compute_latency_stats, find_free_port

# ---------------------------------------------------------------------------
# find_free_port
# ---------------------------------------------------------------------------


def test_find_free_port_returns_valid_port() -> None:
    port = find_free_port()
    assert 1024 <= port <= 65535


def test_find_free_port_returns_different_ports() -> None:
    # Ports may be reused quickly, but two rapid calls should typically differ
    # because the OS won't reassign the same ephemeral port immediately.
    ports = {find_free_port() for _ in range(5)}
    assert len(ports) >= 2


# ---------------------------------------------------------------------------
# compute_latency_stats
# ---------------------------------------------------------------------------


def _make_delivery(created: str, updated: str) -> dict[str, str]:
    return {"created_at": created, "updated_at": updated}


def test_compute_latency_stats_single_delivery() -> None:
    # 1-second latency
    deliveries = [_make_delivery("2024-01-01T10:00:00+00:00", "2024-01-01T10:00:01+00:00")]
    stats = compute_latency_stats(deliveries)
    assert stats.count == 1
    assert stats.p50_ms == pytest.approx(1000.0, rel=0.01)
    assert stats.p95_ms == pytest.approx(1000.0, rel=0.01)
    assert stats.p99_ms == pytest.approx(1000.0, rel=0.01)
    assert stats.mean_ms == pytest.approx(1000.0, rel=0.01)
    assert stats.min_ms == pytest.approx(1000.0, rel=0.01)
    assert stats.max_ms == pytest.approx(1000.0, rel=0.01)


def test_compute_latency_stats_multiple_deliveries() -> None:
    # 100ms, 200ms, 300ms, 400ms, 500ms
    base = "2024-01-01T10:00:00+00:00"
    deliveries = [
        _make_delivery(base, "2024-01-01T10:00:00.100000+00:00"),
        _make_delivery(base, "2024-01-01T10:00:00.200000+00:00"),
        _make_delivery(base, "2024-01-01T10:00:00.300000+00:00"),
        _make_delivery(base, "2024-01-01T10:00:00.400000+00:00"),
        _make_delivery(base, "2024-01-01T10:00:00.500000+00:00"),
    ]
    stats = compute_latency_stats(deliveries)
    assert stats.count == 5
    assert stats.min_ms == pytest.approx(100.0, abs=1.0)
    assert stats.max_ms == pytest.approx(500.0, abs=1.0)
    assert stats.mean_ms == pytest.approx(300.0, abs=1.0)


def test_compute_latency_stats_accepts_z_suffix() -> None:
    # ISO-8601 with Z suffix (as returned by FastAPI/Postgres)
    deliveries = [_make_delivery("2024-06-01T12:00:00Z", "2024-06-01T12:00:00.250000Z")]
    stats = compute_latency_stats(deliveries)
    assert stats.count == 1
    assert stats.p50_ms == pytest.approx(250.0, abs=1.0)


def test_compute_latency_stats_accepts_naive_timestamps() -> None:
    # Naive timestamps (no tz info) are treated as UTC
    deliveries = [_make_delivery("2024-01-01T10:00:00", "2024-01-01T10:00:02")]
    stats = compute_latency_stats(deliveries)
    assert stats.p50_ms == pytest.approx(2000.0, abs=1.0)


def test_compute_latency_stats_returns_latency_stats_instance() -> None:
    deliveries = [_make_delivery("2024-01-01T10:00:00Z", "2024-01-01T10:00:01Z")]
    stats = compute_latency_stats(deliveries)
    assert isinstance(stats, LatencyStats)


def test_compute_latency_stats_empty_raises() -> None:
    with pytest.raises(ValueError, match="No deliveries"):
        compute_latency_stats([])


def test_compute_latency_stats_percentile_ordering() -> None:
    # p50 <= p95 <= p99 always
    base = "2024-01-01T10:00:00Z"
    deliveries = [
        _make_delivery(base, f"2024-01-01T10:00:0{i}.{i * 100:06d}Z") for i in range(1, 5)
    ]
    # just confirm ordering holds
    stats = compute_latency_stats(deliveries)
    assert stats.p50_ms <= stats.p95_ms <= stats.p99_ms
