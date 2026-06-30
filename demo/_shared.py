"""Shared helpers used by all demo scenario scripts.

Each scenario runs self-contained: it provisions its own project + API key +
endpoint via the public API, starts a local receiver and worker subprocess,
runs the scenario, then tears everything down.

Run scenarios from the project root::

    python -m demo.scenario_1_failure_backoff_deadletter

Or via the convenience wrapper::

    bash demo/run_all.sh
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import httpx

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

_TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

# Worker environment overrides used across all demo scenarios.
# Lower attempt limits and shorter backoff make failure scenarios finish in ~15s
# rather than the default 6 attempts × 10-3600s backoff.
WORKER_DEMO_ENV: dict[str, str] = {
    "MAX_DELIVERY_ATTEMPTS": "3",
    "RETRY_BASE_SECONDS": "3",
    "RETRY_CAP_SECONDS": "60",
    "DELIVERY_TIMEOUT_SECONDS": "10",
}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def section(title: str) -> None:
    bar = "=" * 66
    print(f"\n{bar}")
    print(f"  {title}")
    print(f"{bar}")


def divider(title: str = "") -> None:
    if title:
        print(f"\n  ── {title} {'─' * (52 - len(title))}")
    else:
        print(f"\n  {'─' * 56}")


def step(msg: str) -> None:
    print(f"  → {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


def passed(label: str) -> None:
    print(f"\n  ✓ {label} PASSED")


def failed(label: str, reason: str) -> None:
    print(f"\n  ✗ {label} FAILED: {reason}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# API client helpers
# ---------------------------------------------------------------------------


def setup_project(name_prefix: str) -> tuple[str, str]:
    """Create a fresh project + API key; return (project_id, plaintext_api_key)."""
    name = f"{name_prefix}-{int(time.time())}"
    r = httpx.post(f"{API_BASE_URL}/projects", json={"name": name}, timeout=10)
    r.raise_for_status()
    project_id: str = str(r.json()["id"])

    r2 = httpx.post(
        f"{API_BASE_URL}/projects/{project_id}/api-keys",
        json={"name": "demo"},
        timeout=10,
    )
    r2.raise_for_status()
    api_key: str = r2.json()["key"]

    step(f"project {project_id[:8]}…  key {api_key[:13]}…")
    return project_id, api_key


def create_endpoint(
    api_key: str,
    url: str,
    event_types: list[str],
) -> dict[str, Any]:
    """Register a webhook endpoint; return the full response dict (including secret)."""
    r = httpx.post(
        f"{API_BASE_URL}/endpoints",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": url, "event_types": event_types},
        timeout=10,
    )
    r.raise_for_status()
    data: dict[str, Any] = r.json()
    step(f"endpoint {str(data['id'])[:8]}…  url={url}")
    return data


def post_event(
    api_key: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Publish an event; return {event_id, queued_deliveries}."""
    headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    r = httpx.post(
        f"{API_BASE_URL}/events",
        headers=headers,
        json={"type": event_type, "payload": payload},
        timeout=10,
    )
    r.raise_for_status()
    data: dict[str, Any] = r.json()
    idem_note = f" (idempotency_key={idempotency_key!r})" if idempotency_key else ""
    step(f"event {str(data['event_id'])[:8]}…  queued={data['queued_deliveries']}{idem_note}")
    return data


def get_event(api_key: str, event_id: str) -> dict[str, Any]:
    """Fetch event detail with embedded deliveries list."""
    r = httpx.get(
        f"{API_BASE_URL}/events/{event_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def get_delivery(api_key: str, delivery_id: str) -> dict[str, Any]:
    r = httpx.get(
        f"{API_BASE_URL}/deliveries/{delivery_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def list_attempts(api_key: str, delivery_id: str) -> list[dict[str, Any]]:
    r = httpx.get(
        f"{API_BASE_URL}/deliveries/{delivery_id}/attempts",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()
    return result


def do_redrive(api_key: str, delivery_id: str) -> dict[str, Any]:
    r = httpx.post(
        f"{API_BASE_URL}/deliveries/{delivery_id}/redrive",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def wait_for_status(
    api_key: str,
    delivery_id: str,
    target: str,
    *,
    timeout: float = 90,
    poll: float = 0.5,
) -> dict[str, Any]:
    """Poll GET /deliveries/{id} until status == target or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = get_delivery(api_key, delivery_id)
        if d["status"] == target:
            return d
        time.sleep(poll)
    raise TimeoutError(
        f"Delivery {delivery_id} did not reach status={target!r} within {timeout:.0f}s"
    )


def wait_for_any_status(
    api_key: str,
    delivery_id: str,
    targets: set[str],
    *,
    timeout: float = 90,
    poll: float = 0.5,
) -> dict[str, Any]:
    """Poll until delivery status is in *targets*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = get_delivery(api_key, delivery_id)
        if d["status"] in targets:
            return d
        time.sleep(poll)
    raise TimeoutError(
        f"Delivery {delivery_id} did not reach any of {targets} within {timeout:.0f}s"
    )


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def start_receiver(
    port: int,
    *,
    status: int = 200,
    latency: float = 0.0,
    fail_count: int = 0,
) -> subprocess.Popen[bytes]:
    """Start configurable_receiver.py and wait for it to bind."""
    cmd = [
        sys.executable,
        os.path.join(_TOOLS_DIR, "configurable_receiver.py"),
        "--port",
        str(port),
        "--status",
        str(status),
        "--latency",
        str(latency),
        "--fail-count",
        str(fail_count),
    ]
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1.0)  # let uvicorn bind the port
    return proc


def start_worker(extra_env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    """Start the delivery worker subprocess with demo-friendly env overrides."""
    env = {**os.environ, **WORKER_DEMO_ENV, **(extra_env or {})}
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def stop_proc(proc: subprocess.Popen[bytes], name: str = "process") -> None:
    """Terminate a subprocess gracefully, then force-kill after 3 s."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    step(f"stopped {name} (pid {proc.pid})")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def print_attempt_table(attempts: list[dict[str, Any]]) -> None:
    if not attempts:
        info("(no attempts recorded)")
        return
    print()
    print(f"  {'#':<5} {'http':<8} {'ms':<8} error")
    print(f"  {'─' * 5} {'─' * 8} {'─' * 8} {'─' * 40}")
    for a in attempts:
        err = str(a.get("error") or "")[:40]
        print(
            f"  {a['attempt_number']:<5}"
            f" {str(a.get('response_status') or '─'):<8}"
            f" {str(a.get('duration_ms') or '─'):<8}"
            f" {err}"
        )


def print_delivery_summary(d: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
    print()
    print(f"  delivery_id   : {d['id']}")
    print(f"  status        : {d['status']}")
    print(f"  attempt_count : {d['attempt_count']}")
    if d.get("next_attempt_at"):
        print(f"  next_attempt  : {d['next_attempt_at']}")
    if d.get("leased_until"):
        print(f"  leased_until  : {d['leased_until']}")
    print_attempt_table(attempts)
