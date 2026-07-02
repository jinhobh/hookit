"""CLI to seed the shared showcase project (idempotent).

Run once after migrations (locally or as part of deploy) so the live demo has a
project, a Discord endpoint, a controllable receiver, and an API key for the
external producer:

    python -m app.seed_showcase

Reads all configuration from settings/environment (see
``app.core.config.Settings`` — ``SHOWCASE_*`` variables). Safe to re-run.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.showcase import seed_showcase

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    with SessionLocal() as session:
        handles = seed_showcase(session, settings)
        session.commit()
    logger.info(
        "showcase seeded: project=%s receiver_endpoint=%s discord_endpoint=%s",
        handles.project_id,
        handles.receiver_endpoint_id,
        handles.discord_endpoint_id or "(disabled — set SHOWCASE_DISCORD_WEBHOOK_URL)",
    )


if __name__ == "__main__":
    main()
