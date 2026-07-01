"""Prometheus metrics exposition + JSON dashboard summary endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_project
from app.db.session import get_session
from app.models.project import Project
from app.schemas.metrics import MetricsSummaryResponse
from app.services.metrics import delivery_summary

router = APIRouter(tags=["system"])


@router.get("/metrics")
def get_metrics() -> Response:
    """Expose Prometheus metrics in text exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/metrics/summary", response_model=MetricsSummaryResponse, tags=["deliveries"])
def get_metrics_summary(
    project: Project = Depends(get_current_project),
    session: Session = Depends(get_session),
) -> MetricsSummaryResponse:
    """Return aggregate delivery health for the authenticated project.

    Powers the dashboard: status totals, terminal success rate, dead-letter
    depth, total attempts, latency percentiles, and one-minute throughput.
    """
    return delivery_summary(session, project.id)
