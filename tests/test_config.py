"""Tests for application configuration (Pydantic Settings).

Verifies that settings load with typed defaults and that environment variable
overrides take effect.  Uses monkeypatch to avoid polluting the real environment
and clears the lru_cache between tests so each case gets a fresh Settings parse.
"""

from __future__ import annotations

import pytest
from app.core.config import Settings, get_settings


def test_settings_default_values() -> None:
    s = Settings()

    assert s.app_name == "reliable-webhook-platform"
    assert s.app_env == "local"
    assert s.debug is False
    assert s.database_url.startswith("postgresql+psycopg://")


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "overridden-name")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@testhost:5432/testdb")

    s = Settings()

    assert s.app_name == "overridden-name"
    assert s.app_env == "test"
    assert s.debug is True
    assert s.database_url == "postgresql+psycopg://test:test@testhost:5432/testdb"


def test_public_base_url_default() -> None:
    s = Settings()
    assert s.public_base_url == "http://localhost:8000"


def test_public_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://hookit.fly.dev")

    s = Settings()

    assert s.public_base_url == "https://hookit.fly.dev"


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()

    assert s1 is s2


def test_get_settings_cache_clear_produces_new_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    s1 = get_settings()

    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    s2 = get_settings()

    assert s2.app_env == "staging"
    assert s1 is not s2
