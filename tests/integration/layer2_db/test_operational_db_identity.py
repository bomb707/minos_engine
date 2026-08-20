"""Canonical operational-database identity guard against a REAL PostgreSQL 16 server.

Complements the unit contract in ``tests/unit/layer2/test_operational_db_identity.py``:
here the ``current_database()`` values come from actual live connections to real
databases, proving the guard passes on ``minos_engine_db`` and fails (typed) on any
other real database — including one whose name *contains* the canonical string, which a
DSN-substring check would wrongly accept.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from minos_engine.storage.constants import CANONICAL_OPERATIONAL_DATABASE_NAME
from minos_engine.storage.database import (
    OperationalDatabaseIdentityError,
    connected_database_name,
    normalize_database_url,
    verify_operational_database_identity,
)

from .conftest import scratch_database


@contextlib.contextmanager
def _canonical_named_db(base_url: str) -> Iterator[str]:
    """Yield a URL to a real database literally named ``minos_engine_db``.

    If the base URL already points at the canonical database (as in CI, whose service
    database is ``minos_engine_db``) it is used directly — we must never DROP the
    database we are connected through. Otherwise a scratch ``minos_engine_db`` is
    created and dropped."""
    normalized = normalize_database_url(base_url)
    if make_url(normalized).database == CANONICAL_OPERATIONAL_DATABASE_NAME:
        yield normalized
    else:
        with scratch_database(base_url, CANONICAL_OPERATIONAL_DATABASE_NAME) as url:
            yield url


def test_connected_canonical_database_passes(pg_base_url: str) -> None:
    with _canonical_named_db(pg_base_url) as url:
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                assert connected_database_name(conn) == "minos_engine_db"
                assert verify_operational_database_identity(conn) == "minos_engine_db"
        finally:
            engine.dispose()


def test_non_canonical_real_database_fails_typed(pg_base_url: str) -> None:
    # A real database whose name is NOT the canonical one must be rejected with the
    # typed error — decided from the live current_database(), fail-closed. (A dedicated
    # name, disjoint from other suites' session-scoped scratch databases such as
    # minos_l2e_features.)
    with scratch_database(pg_base_url, "minos_id_noncanonical") as url:
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                assert connected_database_name(conn) == "minos_id_noncanonical"
                with pytest.raises(OperationalDatabaseIdentityError):
                    verify_operational_database_identity(conn)
        finally:
            engine.dispose()


def test_prefix_named_real_database_fails_not_substring(pg_base_url: str) -> None:
    # The DSN + database name both CONTAIN "minos_engine_db" as a prefix. A substring
    # match on the URL would wrongly pass; the exact live current_database() check
    # correctly fails. This is the definitive "not DSN string matching" proof.
    with scratch_database(pg_base_url, "minos_engine_db_staging") as url:
        assert "minos_engine_db" in url  # the canonical string is present in the DSN
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                assert connected_database_name(conn) == "minos_engine_db_staging"
                with pytest.raises(OperationalDatabaseIdentityError):
                    verify_operational_database_identity(conn)
        finally:
            engine.dispose()


def test_generic_scratch_database_remains_name_independent(pg_base_url: str) -> None:
    # Generic helpers impose no canonical name: an arbitrarily named scratch database is
    # fully usable and its connected name is simply reported, with no guard applied.
    # (A dedicated name, disjoint from the session-scoped fixtures' scratch databases.)
    with scratch_database(pg_base_url, "minos_id_generic_scratch") as url:
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                assert connected_database_name(conn) == "minos_id_generic_scratch"
        finally:
            engine.dispose()
