"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.middleware import RequestIDFilter, RequestIDMiddleware
from app.routers import deliveries, endpoints, events, me, metrics, projects, showcase

_STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Initialises structured logging before wiring up routers so that all
    components share a consistent log format from the moment the app starts.
    """
    settings = get_settings()

    log_filter = RequestIDFilter()
    handler = logging.StreamHandler()
    handler.addFilter(log_filter)
    handler.setFormatter(
        logging.Formatter(
            "level=%(levelname)s logger=%(name)s request_id=%(request_id)s %(message)s"
        )
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level.upper())
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Reliable Webhook Delivery Platform — backend service.",
    )

    application.add_middleware(RequestIDMiddleware)

    application.include_router(me.router)
    application.include_router(projects.router)
    application.include_router(endpoints.router)
    application.include_router(events.router)
    application.include_router(deliveries.router)
    application.include_router(metrics.router)
    application.include_router(showcase.router)

    _seed_showcase_best_effort()

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness/readiness probe — no database calls."""
        return {"status": "ok"}

    # Static, read-only observability dashboard (single-page vanilla JS). Served
    # last so it never shadows an API route; `html=True` serves index.html at
    # `/dashboard/`. The page authenticates against the JSON API with a project
    # API key supplied by the viewer, so no data is exposed unauthenticated.
    application.mount(
        "/dashboard",
        StaticFiles(directory=_STATIC_DIR, html=True),
        name="dashboard",
    )

    return application


def _seed_showcase_best_effort() -> None:
    """Seed the shared showcase project at startup, tolerating an absent database.

    The live demo needs its project/endpoints/key to exist; seeding is idempotent
    so running it on every boot is safe. It must never crash startup, though —
    ``/health`` must stay up even if the database is unreachable — so any failure
    is logged and swallowed (the ``/showcase/*`` routes also self-heal on demand).
    """
    from app.db.session import SessionLocal
    from app.services.showcase import seed_showcase

    try:
        with SessionLocal() as session:
            seed_showcase(session)
            session.commit()
    except Exception as exc:  # noqa: BLE001 — startup must not fail on DB issues
        logger.warning("showcase seeding skipped at startup: %s", exc)


app = create_app()
