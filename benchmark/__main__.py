"""Entry point: python -m benchmark [options].

Provisions a local environment, publishes N events, runs the delivery worker,
and prints a throughput + latency summary.

Requires the FastAPI app to be running (default: http://localhost:8000).
The delivery worker is started automatically as a subprocess.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from benchmark.runner import (
    collect_succeeded,
    compute_latency_stats,
    find_free_port,
    print_summary,
    provision,
    publish_events,
    start_sink,
    start_worker,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark",
        description=(
            "Benchmark the delivery worker: publish N events, measure throughput and latency.\n"
            "\n"
            "Requires the FastAPI app running at --api-base (default http://localhost:8000).\n"
            "The delivery worker is started automatically; Postgres must be up and migrated."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--events",
        type=int,
        default=500,
        metavar="N",
        help="Number of events to publish (default: 500)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        metavar="C",
        help="Concurrent publish requests (default: 10)",
    )
    parser.add_argument(
        "--sink-port",
        type=int,
        default=0,
        metavar="PORT",
        help="Port for the local sink receiver; 0 = pick automatically (default: 0)",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("API_BASE_URL", "http://localhost:8000"),
        metavar="URL",
        help="API base URL (env: API_BASE_URL, default: http://localhost:8000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="S",
        help="Seconds to wait for all deliveries (default: 120)",
    )
    args = parser.parse_args()

    api_base = args.api_base.rstrip("/")
    sink_port: int = args.sink_port or find_free_port()
    sink_url = f"http://localhost:{sink_port}/"

    print()
    print(f"  events      : {args.events}  (concurrency={args.concurrency})")
    print(f"  api base    : {api_base}")
    print(f"  sink url    : {sink_url}")

    # Verify API is reachable before proceeding.
    try:
        httpx.get(f"{api_base}/health", timeout=5).raise_for_status()
    except Exception as exc:
        print(
            f"\nERROR: cannot reach {api_base}/health: {exc}\n"
            "  Start the app first:  uvicorn app.main:app --reload",
            file=sys.stderr,
        )
        sys.exit(1)

    import time

    # 1. Start the fast local sink receiver.
    print("\n  [1/5] starting sink receiver...")
    sink_proc = start_sink(sink_port)

    worker_proc = None
    try:
        # 2. Provision project + API key + endpoint.
        print("  [2/5] provisioning project, API key, and endpoint...")
        api_key, _project_id = provision(api_base, sink_url)

        # 3. Publish events.
        print(f"  [3/5] publishing {args.events} events...")
        publish_elapsed = publish_events(api_base, api_key, args.events, args.concurrency)
        publish_rate = args.events / publish_elapsed if publish_elapsed > 0 else 0.0
        print(f"        done in {publish_elapsed:.2f}s ({publish_rate:.1f} events/sec)")

        # 4. Start worker and wait for all deliveries to succeed.
        print("  [4/5] starting worker, waiting for deliveries...")
        t_worker_start = time.monotonic()
        worker_proc = start_worker()

        deliveries = collect_succeeded(api_base, api_key, args.events, timeout=args.timeout)
        wall_time = time.monotonic() - t_worker_start

        # 5. Compute and display stats.
        print("  [5/5] computing statistics...")
        stats = compute_latency_stats(deliveries)
        throughput = len(deliveries) / wall_time if wall_time > 0 else 0.0

        print_summary(
            events=args.events,
            concurrency=args.concurrency,
            api_base=api_base,
            sink_url=sink_url,
            succeeded=len(deliveries),
            wall_time_s=wall_time,
            throughput=throughput,
            stats=stats,
        )

    finally:
        if worker_proc is not None and worker_proc.poll() is None:
            worker_proc.terminate()
            worker_proc.wait(timeout=5)
        sink_proc.terminate()
        sink_proc.wait(timeout=5)


if __name__ == "__main__":
    main()
