"""Tests for RequestIDMiddleware."""

from __future__ import annotations

import uuid

from app.main import app  # noqa: E402
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_returns_x_request_id_header() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert "x-request-id" in response.headers


def test_x_request_id_is_valid_uuid4() -> None:
    response = client.get("/health")
    header_value = response.headers["x-request-id"]
    parsed = uuid.UUID(header_value, version=4)
    assert str(parsed) == header_value


def test_each_request_gets_distinct_request_id() -> None:
    r1 = client.get("/health")
    r2 = client.get("/health")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]
