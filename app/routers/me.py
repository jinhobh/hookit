"""Probe route for verifying API key authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_project
from app.models.project import Project

router = APIRouter()


@router.get("/me", tags=["auth"])
def get_me(project: Project = Depends(get_current_project)) -> dict[str, str]:
    """Return the authenticated project's ID.

    Useful as a liveness probe for API key validity.
    """
    return {"project_id": str(project.id)}
