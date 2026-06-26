"""Tests for the health endpoint.

Uses Starlette's ``TestClient`` (backed by httpx) so no running server is
required. This is the canonical example of the test style expected from agents:
fast, deterministic, and dependency-free where possible.
"""

from __future__ import annotations

from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
