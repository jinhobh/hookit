"""Prometheus metrics exposition endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["system"])


@router.get("/metrics")
def get_metrics() -> Response:
    """Expose Prometheus metrics in text exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
