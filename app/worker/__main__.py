"""Delivery worker entrypoint.

Start with:  python -m app.worker
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

import httpx
import psycopg

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.worker.delivery_worker import run_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
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


def main() -> None:
    settings = get_settings()
    logger.info("Delivery worker starting")
    listen_conn: psycopg.Connection[Any] | None = None
    with httpx.Client() as http_client:
        while True:
            # Ensure we have a LISTEN connection.
            if listen_conn is None:
                try:
                    listen_conn = _open_listen_conn(settings)
                    logger.info("LISTEN established on channel %r", settings.worker_listen_channel)
                except Exception:
                    logger.warning(
                        "Could not open LISTEN connection; falling back to polling",
                        exc_info=True,
                    )

            # Process due deliveries.
            n = 0
            session = SessionLocal()
            try:
                n = run_once(session, http_client)
                session.commit()
                if n:
                    logger.info("Processed %d delivery/deliveries", n)
            except Exception:
                session.rollback()
                logger.exception("Worker loop error")
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


if __name__ == "__main__":
    main()
