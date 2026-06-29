"""Tests for the health endpoint and the application factory."""

from __future__ import annotations

from app.main import app, create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_returns_fastapi_instance() -> None:
    application = create_app()

    assert isinstance(application, FastAPI)


def test_create_app_health_endpoint() -> None:
    application = create_app()
    test_client = TestClient(application)

    response = test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
