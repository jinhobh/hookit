"""FastAPI application entrypoint.

This is intentionally minimal: it exposes only a health endpoint. The webhook
delivery product is built incrementally through the issue queue (see
``docs/ROADMAP.md``). Do not add product logic here without a corresponding
issue.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.core.config import get_settings
from app.routers import endpoints, events, me

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Reliable Webhook Delivery Platform — backend service.",
)


app.include_router(me.router)
app.include_router(endpoints.router)
app.include_router(events.router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Liveness/readiness probe.

    Returns a static ``{"status": "ok"}`` payload. This endpoint must remain
    dependency-free (no database calls) so it can be used as a basic liveness
    check before infrastructure is provisioned.
    """

    return {"status": "ok"}
