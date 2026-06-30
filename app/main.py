"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.core.config import get_settings
from app.middleware import RequestIDFilter, RequestIDMiddleware
from app.routers import deliveries, endpoints, events, me, projects


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

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness/readiness probe — no database calls."""
        return {"status": "ok"}

    return application


app = create_app()
