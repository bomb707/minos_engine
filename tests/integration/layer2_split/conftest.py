"""Real PostgreSQL 16 fixtures for L2-C (SPLIT-FROZEN) integration tests.

Reuses the L2-B helpers (``scratch_database``/``alembic_upgrade``) and provisions a
session database upgraded to the L2-C head (``0002``) with the synthetic 75-sample
manifest persisted as the owner. SQLite is never used; tests skip locally only when
neither ``MINOS_DATABASE_URL`` nor bundled ``pgserver`` is available.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine

from minos_engine.storage.database import create_db_engine, normalize_database_url
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database

_ENV = "MINOS_DATABASE_URL"


@pytest.fixture(scope="session")
def pg_base_url() -> Iterator[str]:
    url = os.environ.get(_ENV)
    if url:
        yield url
        return
    try:
        import pgserver
    except Exception:  # pragma: no cover - only when neither CI nor pgserver present
        pytest.skip("no MINOS_DATABASE_URL and pgserver is not installed")
    tmp = tempfile.mkdtemp(prefix="minos_l2c_")
    server = pgserver.get_server(tmp)
    try:
        yield server.get_uri()
    finally:
        with contextlib.suppress(Exception):
            server.cleanup()


@pytest.fixture(scope="session")
def l2c_main_url(pg_base_url: str) -> Iterator[str]:
    with scratch_database(pg_base_url, "minos_l2c_main") as url:
        alembic_upgrade(url, "0008_l2f_execution_results")
        from minos_engine.storage.dataset_split import persist_manifest
        from tests.layer2c_synth import synthetic_manifest

        eng = create_engine(normalize_database_url(url))
        try:
            with eng.begin() as conn:
                persist_manifest(conn, synthetic_manifest())
        finally:
            eng.dispose()
        yield url


@pytest.fixture(scope="session")
def l2c_engine(l2c_main_url: str) -> Iterator[Engine]:
    eng = create_db_engine(l2c_main_url)
    try:
        yield eng
    finally:
        eng.dispose()
