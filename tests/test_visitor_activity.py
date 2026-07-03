"""Tests for real-visitor activity tracking (idle watchdog's DB-backed signal).

Two tiers, mirroring tests/test_showcase.py: a savepoint-isolated session for
the service-level touch/get helpers, and a real TestClient against an isolated
showcase project to confirm which routes actually touch it.

All tests require a live Postgres instance; skipped automatically when unreachable.
"""

from __future__ import annotations

import uuid

from app.core.config import Settings, get_settings
from app.main import app
from app.services.showcase import get_last_visitor_seen, seed_showcase, touch_visitor_seen
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ===========================================================================
# Service tier: savepoint-isolated session (fixture shared via conftest)
# ===========================================================================


def _settings(**overrides: object) -> Settings:
    base = get_settings()
    defaults: dict[str, object] = {
        "showcase_project_name": f"__showcase_test_{uuid.uuid4().hex[:10]}__",
        "showcase_discord_webhook_url": "",
        "showcase_api_key": "",
        "database_url": base.database_url,
        "public_base_url": "http://localhost:8000",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_get_last_visitor_seen_is_none_before_any_touch(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())
    assert get_last_visitor_seen(sc_session, handles.project_id) is None


def test_touch_visitor_seen_creates_then_updates(sc_session: Session) -> None:
    handles = seed_showcase(sc_session, _settings())

    touch_visitor_seen(sc_session, handles.project_id)
    first = get_last_visitor_seen(sc_session, handles.project_id)
    assert first is not None

    touch_visitor_seen(sc_session, handles.project_id)
    second = get_last_visitor_seen(sc_session, handles.project_id)
    assert second is not None
    assert second >= first


# ===========================================================================
# Integration tier: real TestClient against an isolated showcase project
# (isolated_showcase fixture shared via conftest)
# ===========================================================================


def test_dashboard_get_routes_record_visitor_activity(isolated_showcase: str) -> None:
    with TestClient(app) as client:
        from app.db.session import SessionLocal
        from app.services.showcase import resolve_showcase

        # Seed explicitly: the showcase is only created lazily on the first
        # route call, but calling a route would also record visitor activity,
        # defeating the pre-condition check below.
        with SessionLocal() as session:
            seed_showcase(session, get_settings())
            session.commit()

        with SessionLocal() as session:
            handles = resolve_showcase(session)
            assert handles is not None
            assert get_last_visitor_seen(session, handles.project_id) is None

        for path in ("/showcase/summary", "/showcase/feed", "/showcase/deliveries"):
            assert client.get(path).status_code == 200
            with SessionLocal() as session:
                assert get_last_visitor_seen(session, handles.project_id) is not None


def test_receiver_post_does_not_record_visitor_activity(isolated_showcase: str) -> None:
    with TestClient(app) as client:
        from app.db.session import SessionLocal
        from app.services.showcase import resolve_showcase

        # Seed explicitly so resolve_showcase returns handles before any route
        # request (the showcase is otherwise seeded lazily by the first route call).
        with SessionLocal() as session:
            seed_showcase(session, get_settings())
            session.commit()

        with SessionLocal() as session:
            handles = resolve_showcase(session)
            assert handles is not None
            receiver_id = handles.receiver_endpoint_id

        # No signature/verification needed: an unverified POST still exercises
        # the route (401) without touching visitor activity either way.
        client.post(f"/showcase/receiver/{receiver_id}", content=b"{}")

        with SessionLocal() as session:
            assert get_last_visitor_seen(session, handles.project_id) is None
