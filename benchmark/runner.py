"""Core benchmark logic: sink receiver, provisioning, publishing, and stats."""

from __future__ import annotations

import concurrent.futures
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx


def find_free_port() -> int:
    """Return an available TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_sink(port: int) -> subprocess.Popen[bytes]:
    """Start configurable_receiver.py as the fast local sink; return its Popen handle."""
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        [
            sys.executable,
            os.path.join(tools_dir, "configurable_receiver.py"),
            "--port",
            str(port),
            "--status",
            "200",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)  # let uvicorn bind the port
    return proc


def start_worker(extra_env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    """Start the delivery worker subprocess; return its Popen handle."""
    env = {**os.environ, **(extra_env or {})}
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def provision(api_base: str, sink_url: str) -> tuple[str, str]:
    """Create a project, API key, and endpoint; return (api_key, project_id)."""
    ts = int(time.time())
    r = httpx.post(f"{api_base}/projects", json={"name": f"benchmark-{ts}"}, timeout=10)
    r.raise_for_status()
    project_id: str = r.json()["id"]

    r = httpx.post(
        f"{api_base}/projects/{project_id}/api-keys",
        json={"name": "bench"},
        timeout=10,
    )
    r.raise_for_status()
    api_key: str = r.json()["key"]

    headers = {"Authorization": f"Bearer {api_key}"}
    r = httpx.post(
        f"{api_base}/endpoints",
        headers=headers,
        json={"url": sink_url, "event_types": ["benchmark.event"], "status": "active"},
        timeout=10,
    )
    r.raise_for_status()
    return api_key, project_id


def publish_events(api_base: str, api_key: str, n: int, concurrency: int) -> float:
    """Publish *n* events at up to *concurrency* concurrent requests.

    Returns the total wall-clock time in seconds.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    errors: list[str] = []
    lock = threading.Lock()

    def publish_one(idx: int) -> None:
        try:
            r = httpx.post(
                f"{api_base}/events",
                headers=headers,
                json={"type": "benchmark.event", "payload": {"idx": idx}},
                timeout=30,
            )
            r.raise_for_status()
        except Exception as exc:
            with lock:
                errors.append(str(exc))

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(publish_one, i) for i in range(n)]
        concurrent.futures.wait(futures)
    elapsed = time.monotonic() - t0

    if errors:
        print(f"  WARNING: {len(errors)} publish error(s): {errors[:3]}", file=sys.stderr)
    return elapsed


def collect_succeeded(
    api_base: str,
    api_key: str,
    expected: int,
    *,
    timeout: float = 120.0,
    poll: float = 0.5,
) -> list[dict[str, Any]]:
    """Poll until *expected* deliveries have status=succeeded; return them all."""
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.monotonic() + timeout
    items: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        time.sleep(poll)
        items = _fetch_all_succeeded(api_base, headers)
        print(f"\r  waiting... {len(items)}/{expected} succeeded", end="", flush=True)
        if len(items) >= expected:
            print()
            return items

    print()
    raise TimeoutError(f"Only {len(items)}/{expected} deliveries succeeded after {timeout:.0f}s")


def _fetch_all_succeeded(api_base: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """Fetch all succeeded deliveries, following pagination cursors."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"status": "succeeded", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = httpx.get(f"{api_base}/deliveries", headers=headers, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        items.extend(data["items"])
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return items


class LatencyStats:
    """Container for latency percentile stats (all values in milliseconds)."""

    __slots__ = ("count", "p50_ms", "p95_ms", "p99_ms", "mean_ms", "min_ms", "max_ms")

    def __init__(
        self,
        *,
        count: int,
        p50_ms: float,
        p95_ms: float,
        p99_ms: float,
        mean_ms: float,
        min_ms: float,
        max_ms: float,
    ) -> None:
        self.count = count
        self.p50_ms = p50_ms
        self.p95_ms = p95_ms
        self.p99_ms = p99_ms
        self.mean_ms = mean_ms
        self.min_ms = min_ms
        self.max_ms = max_ms


def compute_latency_stats(deliveries: list[dict[str, Any]]) -> LatencyStats:
    """Compute p50/p95/p99 latency from delivery created_at → updated_at intervals.

    Args:
        deliveries: list of delivery dicts as returned by GET /deliveries, each
                    with ISO-8601 ``created_at`` and ``updated_at`` fields.

    Returns:
        A LatencyStats instance with all times in milliseconds.

    Raises:
        ValueError: if *deliveries* is empty.
    """
    if not deliveries:
        raise ValueError("No deliveries to compute stats from")

    latencies_ms: list[float] = []
    for d in deliveries:
        created = _parse_iso(d["created_at"])
        updated = _parse_iso(d["updated_at"])
        ms = (updated - created).total_seconds() * 1000.0
        if ms >= 0:
            latencies_ms.append(ms)

    if not latencies_ms:
        raise ValueError("No valid (non-negative) latency samples found")

    latencies_ms.sort()
    n = len(latencies_ms)

    def pct(p: float) -> float:
        idx = min(int(p / 100.0 * n), n - 1)
        return latencies_ms[idx]

    return LatencyStats(
        count=n,
        p50_ms=pct(50),
        p95_ms=pct(95),
        p99_ms=pct(99),
        mean_ms=statistics.mean(latencies_ms),
        min_ms=latencies_ms[0],
        max_ms=latencies_ms[-1],
    )


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def print_summary(
    *,
    events: int,
    concurrency: int,
    api_base: str,
    sink_url: str,
    succeeded: int,
    wall_time_s: float,
    throughput: float,
    stats: LatencyStats,
) -> None:
    """Print a compact benchmark result table to stdout."""
    bar = "=" * 54
    print(f"\n{bar}")
    print("  BENCHMARK RESULTS")
    print(f"{bar}")
    print(f"  api base   : {api_base}")
    print(f"  sink       : {sink_url}")
    print(f"  events     : {events}  (concurrency={concurrency})")
    print(f"  succeeded  : {succeeded}")
    print(f"  wall time  : {wall_time_s:.2f}s  (worker start → all delivered)")
    print(f"  throughput : {throughput:.1f} deliveries/sec")
    print()
    print(f"  {'metric':<22} {'value':>10}")
    print(f"  {'─' * 22} {'─' * 10}")
    print(f"  {'latency p50':<22} {stats.p50_ms:>8.0f}ms")
    print(f"  {'latency p95':<22} {stats.p95_ms:>8.0f}ms")
    print(f"  {'latency p99':<22} {stats.p99_ms:>8.0f}ms")
    print(f"  {'latency mean':<22} {stats.mean_ms:>8.0f}ms")
    print(f"  {'latency min':<22} {stats.min_ms:>8.0f}ms")
    print(f"  {'latency max':<22} {stats.max_ms:>8.0f}ms")
    print(f"{bar}")
    print()
    print(
        "  Note: figures are single-worker on a dev box; the architecture"
        " supports horizontal scaling by running multiple worker processes."
    )
    print()
