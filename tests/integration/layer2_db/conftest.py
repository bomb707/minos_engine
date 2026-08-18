"""Real PostgreSQL 16 fixtures for L2-B integration tests.

Uses ``MINOS_DATABASE_URL`` when set (GitHub CI service container) and otherwise an
ephemeral bundled PostgreSQL 16 server (``pgserver``). Tests skip locally only when
neither is available; in CI they must run (enforced by ``test_ci_guard``). SQLite is
never used.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from minos_engine.storage.database import (
    create_db_engine,
    make_session_factory,
    normalize_database_url,
)

_ENV = "MINOS_DATABASE_URL"


def postgres_base_url() -> str | None:
    """The admin/superuser base URL, or None when no PostgreSQL is available."""
    url = os.environ.get(_ENV)
    if url:
        return url
    try:
        import pgserver  # noqa: F401
    except Exception:
        return None
    return "pgserver"


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
    tmp = tempfile.mkdtemp(prefix="minos_l2b_")
    server = pgserver.get_server(tmp)
    try:
        yield server.get_uri()
    finally:
        with contextlib.suppress(Exception):
            server.cleanup()


def _swap_db(base_url: str, name: str) -> str:
    return (
        make_url(normalize_database_url(base_url))
        .set(database=name)
        .render_as_string(hide_password=False)
    )


def alembic_upgrade(url: str, revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    prev = os.environ.get(_ENV)
    os.environ[_ENV] = url
    try:
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        if prev is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = prev


def alembic_downgrade(url: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    prev = os.environ.get(_ENV)
    os.environ[_ENV] = url
    try:
        command.downgrade(Config("alembic.ini"), revision)
    finally:
        if prev is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = prev


@contextlib.contextmanager
def scratch_database(base_url: str, name: str) -> Iterator[str]:
    """Create a fresh database, yield its URL, and drop it afterwards (real server)."""
    admin = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    try:
        yield _swap_db(base_url, name)
    finally:
        admin2 = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
        with admin2.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin2.dispose()


@pytest.fixture(scope="session")
def main_db_url(pg_base_url: str) -> Iterator[str]:
    # Pin the L2-B suite to the L2-B revision so a later stage's migration (L2-C's
    # 0002 and beyond) never changes what these L2-B tests observe (exactly 10 tables,
    # 7 schemas, head 0001_l2b_initial). L2-C validates itself in tests/integration/
    # layer2_split against the current head.
    with scratch_database(pg_base_url, "minos_l2b_main") as url:
        alembic_upgrade(url, "0001_l2b_initial")
        yield url


@pytest.fixture(scope="session")
def engine(main_db_url: str) -> Iterator[Engine]:
    eng = create_db_engine(main_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine):
    return make_session_factory(engine)


@pytest.fixture
def rollback_conn(engine: Engine) -> Iterator[Connection]:
    """A connection wrapped in a transaction that is always rolled back (isolation)."""
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        trans.rollback()
        conn.close()
