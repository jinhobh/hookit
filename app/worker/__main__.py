"""Delivery worker entrypoint.

Start with:  python -m app.worker

Runs ``WORKER_CONCURRENCY`` independent claim loops in one process (default 1).
Each loop has its own name (``<worker_name>#<i>``), its own LISTEN connection,
and opens its own DB session per tick — so concurrent claiming exercises
``FOR UPDATE SKIP LOCKED`` across real sessions without extra machines.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

import httpx
import psycopg

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.worker.delivery_worker import default_worker_name, run_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# httpx logs each request's full URL at INFO; outbound webhook URLs can embed
# credentials (e.g. a Discord webhook token), so keep it at WARNING to avoid
# leaking secrets into logs (see CLAUDE.md §9).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _open_listen_conn(settings: Settings) -> psycopg.Connection[Any]:
    """Open a raw psycopg connection with autocommit and issue LISTEN."""
    dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    conn: psycopg.Connection[Any] = psycopg.connect(dsn, autocommit=True)
    conn.execute(f"LISTEN {settings.worker_listen_channel}")
    return conn


def _wait_for_notify(conn: psycopg.Connection[Any], timeout: float) -> None:
    """Block until a notification arrives on the channel or *timeout* seconds elapse."""
    for _ in conn.notifies(timeout=timeout):
        break


def _worker_loop(settings: Settings, worker_name: str) -> None:
    """One independent claim loop: own LISTEN connection, own session per tick."""
    logger.info("Delivery worker loop starting worker_name=%s", worker_name)
    listen_conn: psycopg.Connection[Any] | None = None
    with httpx.Client() as http_client:
        while True:
            # Ensure we have a LISTEN connection.
            if listen_conn is None:
                try:
                    listen_conn = _open_listen_conn(settings)
                    logger.info(
                        "LISTEN established on channel %r worker_name=%s",
                        settings.worker_listen_channel,
                        worker_name,
                    )
                except Exception:
                    logger.warning(
                        "Could not open LISTEN connection; falling back to polling",
                        exc_info=True,
                    )

            # Process due deliveries.
            n = 0
            session = SessionLocal()
            try:
                n = run_once(session, http_client, worker_name=worker_name)
                session.commit()
                if n:
                    logger.info("Processed %d delivery/deliveries worker_name=%s", n, worker_name)
            except Exception:
                session.rollback()
                logger.exception("Worker loop error worker_name=%s", worker_name)
            finally:
                session.close()

            if n == 0:
                # Idle — wait for a notify or the fallback poll interval.
                if listen_conn is not None:
                    try:
                        _wait_for_notify(listen_conn, settings.worker_fallback_poll_seconds)
                    except Exception:
                        logger.warning("LISTEN connection lost; will reconnect", exc_info=True)
                        with contextlib.suppress(Exception):
                            listen_conn.close()
                        listen_conn = None
                        time.sleep(1)
                else:
                    time.sleep(settings.worker_fallback_poll_seconds)


def main() -> None:
    settings = get_settings()
    base_name = settings.worker_name or default_worker_name()
    concurrency = settings.worker_concurrency
    logger.info("Delivery worker starting: %d claim loop(s), base name %s", concurrency, base_name)

    if concurrency == 1:
        _worker_loop(settings, base_name)
        return

    names = [f"{base_name}#{i + 1}" for i in range(concurrency)]
    threads = [
        threading.Thread(target=_worker_loop, args=(settings, name), name=name, daemon=True)
        for name in names[1:]
    ]
    for thread in threads:
        thread.start()
    # Run the first loop on the main thread so the process lives with its loops.
    _worker_loop(settings, names[0])


if __name__ == "__main__":
    main()
