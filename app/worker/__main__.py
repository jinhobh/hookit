"""Delivery worker entrypoint.

Start with:  python -m app.worker
"""

from __future__ import annotations

import logging
import time

import httpx

from app.db.session import SessionLocal
from app.worker.delivery_worker import run_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Delivery worker starting")
    with httpx.Client() as http_client:
        while True:
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
                time.sleep(1)


if __name__ == "__main__":
    main()
