"""Tests for Project/ApiKey ORM models and the generate_api_key helper.

Unit tests (no database required) cover key generation and hashing.
Integration tests (require Postgres) cover model persistence via the ORM.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Generator

import pytest
from app.db.base import Base
from app.models.api_key import ApiKey, generate_api_key
from app.models.project import Project
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Unit tests — no DB required
# ---------------------------------------------------------------------------


def test_generate_api_key_returns_three_parts() -> None:
    plaintext, prefix, key_hash = generate_api_key()
    assert plaintext
    assert prefix
    assert key_hash


def test_generate_api_key_prefix_is_prefix_of_plaintext() -> None:
    plaintext, prefix, _ = generate_api_key()
    assert plaintext.startswith(prefix)


def test_generate_api_key_hash_matches_plaintext() -> None:
    plaintext, _, key_hash = generate_api_key()
    expected = hashlib.sha256(plaintext.encode()).hexdigest()
    assert key_hash == expected


def test_generate_api_key_hash_is_sha256_hex_length() -> None:
    _, _, key_hash = generate_api_key()
    assert len(key_hash) == 64
    assert all(c in "0123456789abcdef" for c in key_hash)


def test_generate_api_key_plaintext_starts_with_prefix_token() -> None:
    plaintext, _, _ = generate_api_key()
    assert plaintext.startswith("whk_")


def test_generate_api_key_prefix_starts_with_prefix_token() -> None:
    _, prefix, _ = generate_api_key()
    assert prefix.startswith("whk_")


def test_generate_api_key_each_call_is_unique() -> None:
    results = [generate_api_key() for _ in range(5)]
    plaintexts = [r[0] for r in results]
    assert len(set(plaintexts)) == 5, "expected unique plaintexts for every call"


def test_generate_api_key_plaintext_not_equal_to_hash() -> None:
    plaintext, _, key_hash = generate_api_key()
    assert plaintext != key_hash


# ---------------------------------------------------------------------------
# Integration tests — require a live Postgres instance
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Transactional session that rolls back after every test."""
    Base.metadata.create_all(db_engine)
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def test_project_persists(db_session: Session) -> None:
    project = Project(name="acme")
    db_session.add(project)
    db_session.flush()

    fetched = db_session.get(Project, project.id)
    assert fetched is not None
    assert fetched.name == "acme"
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


def test_project_name_must_be_unique(db_session: Session) -> None:
    db_session.add(Project(name="dupe"))
    db_session.flush()

    db_session.add(Project(name="dupe"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_api_key_persists_with_project(db_session: Session) -> None:
    project = Project(name="beta-corp")
    db_session.add(project)
    db_session.flush()

    _, prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        project_id=project.id,
        name="ci-token",
        key_prefix=prefix,
        key_hash=key_hash,
    )
    db_session.add(api_key)
    db_session.flush()

    fetched = db_session.get(ApiKey, api_key.id)
    assert fetched is not None
    assert fetched.project_id == project.id
    assert fetched.key_prefix == prefix
    assert fetched.key_hash == key_hash
    assert fetched.name == "ci-token"


def test_api_key_project_relationship(db_session: Session) -> None:
    project = Project(name="gamma-llc")
    db_session.add(project)
    db_session.flush()

    _, prefix, key_hash = generate_api_key()
    api_key = ApiKey(project_id=project.id, key_prefix=prefix, key_hash=key_hash)
    db_session.add(api_key)
    db_session.flush()

    db_session.expire(api_key)
    assert api_key.project.name == "gamma-llc"


def test_api_key_hash_must_be_unique(db_session: Session) -> None:
    project = Project(name="delta-inc")
    db_session.add(project)
    db_session.flush()

    _, prefix, key_hash = generate_api_key()
    db_session.add(ApiKey(project_id=project.id, key_prefix=prefix, key_hash=key_hash))
    db_session.flush()

    db_session.add(ApiKey(project_id=project.id, key_prefix=prefix, key_hash=key_hash))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_api_key_cascade_deleted_with_project(db_session: Session) -> None:
    project = Project(name="epsilon-co")
    db_session.add(project)
    db_session.flush()

    _, prefix, key_hash = generate_api_key()
    api_key = ApiKey(project_id=project.id, key_prefix=prefix, key_hash=key_hash)
    db_session.add(api_key)
    db_session.flush()
    api_key_id = api_key.id

    db_session.delete(project)
    db_session.flush()

    assert db_session.get(ApiKey, api_key_id) is None


def test_api_key_project_id_fk_enforced(db_session: Session) -> None:
    _, prefix, key_hash = generate_api_key()
    orphan = ApiKey(
        project_id=uuid.uuid4(),
        key_prefix=prefix,
        key_hash=key_hash,
    )
    db_session.add(orphan)
    with pytest.raises(IntegrityError):
        db_session.flush()
