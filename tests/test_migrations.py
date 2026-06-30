"""Regression test for running the Alembic migration chain end to end.

Every other DB-dependent fixture in this suite builds the schema via
``Base.metadata.create_all``, which never exercises the migrations
themselves. That gap let a bug slip through where ``sa.Enum(...,
create_type=False)`` silently ignored ``create_type`` (a kwarg only
``postgresql.ENUM`` understands), so ``alembic upgrade head`` against a
*fresh* database emitted ``CREATE TYPE`` twice and failed with
``type "endpoint_status" already exists`` (see PR #75). This test runs the
real migration chain against an empty schema so that class of bug is caught
by CI instead of surfacing during a manual deploy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.db.base import Base
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def clean_db(db_engine: Engine) -> Engine:
    """Drop every table/type known to the ORM so the database starts empty."""
    Base.metadata.drop_all(db_engine)
    with db_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    return db_engine


def test_alembic_upgrade_head_succeeds_on_a_fresh_database(clean_db: Engine) -> None:
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))

    command.upgrade(alembic_cfg, "head")

    script = ScriptDirectory.from_config(alembic_cfg)
    expected_head = script.get_current_head()

    with clean_db.connect() as connection:
        current_revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()

    assert current_revision == expected_head

    table_names = set(inspect(clean_db).get_table_names())
    assert {
        "projects",
        "api_keys",
        "endpoints",
        "events",
        "deliveries",
        "delivery_attempts",
        "idempotency_records",
    } <= table_names
