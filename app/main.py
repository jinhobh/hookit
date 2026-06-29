"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.core.config import get_settings
from app.routers import deliveries, endpoints, events, me


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Initialises structured logging before wiring up routers so that all
    components share a consistent log format from the moment the app starts.
    """
    settings = get_settings()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="level=%(levelname)s logger=%(name)s %(message)s",
    )

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Reliable Webhook Delivery Platform — backend service.",
    )

    application.include_router(me.router)
    application.include_router(endpoints.router)
    application.include_router(events.router)
    application.include_router(deliveries.router)

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness/readiness probe — no database calls."""
        return {"status": "ok"}

    return application


app = create_app()
