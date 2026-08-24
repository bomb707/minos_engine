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
        _tune_ephemeral_cluster(server.get_uri())
        yield server.get_uri()
    finally:
        with contextlib.suppress(Exception):
            server.cleanup()


def _tune_ephemeral_cluster(base_url: str) -> None:
    """Disable durability niceties on a THROWAWAY test cluster (never the CI service container).

    The data is deleted at session end, so synchronous commit buys nothing here and costs real
    wall-clock on every one of the thousands of small commits the suite performs.
    """
    with contextlib.suppress(Exception):
        admin = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as c:
                c.execute(text("ALTER SYSTEM SET synchronous_commit = off"))
                c.execute(text("ALTER SYSTEM SET full_page_writes = off"))
                c.execute(text("SELECT pg_reload_conf()"))
        finally:
            admin.dispose()


def _swap_db(base_url: str, name: str) -> str:
    return (
        make_url(normalize_database_url(base_url))
        .set(database=name)
        .render_as_string(hide_password=False)
    )


#: scratch URL -> the base URL it was created from, so the template fast path can find the
#: cluster's maintenance connection without changing any call site.
_SCRATCH_BASE: dict[str, str] = {}

#: (normalized base URL, revision) -> template database name, built ONCE per session by running
#: the REAL migration chain, then cloned per test via CREATE DATABASE ... TEMPLATE (~100ms).
_TEMPLATE_CACHE: dict[tuple[str, str], str] = {}


def _alembic_upgrade_real(url: str, revision: str) -> None:
    """The genuine Alembic migration run. Templates are built through this, so every revision's
    DDL is still truly executed at least once per session per cluster."""
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


def _clone_from_template(url: str, revision: str) -> bool:
    """Fast path: rebuild a FRESH scratch database from a session template already migrated to
    ``revision``, instead of replaying the whole migration chain for the Nth time.

    Strictly fail-open: any doubt (unknown base, database not fresh, template build error,
    clone error) returns False and the caller falls back to the real migration path, so
    correctness never depends on this optimization. Staged upgrades (0006 -> seed -> 0008),
    downgrades and post-downgrade re-upgrades keep using real Alembic because their target
    databases are not fresh.
    """
    base = _SCRATCH_BASE.get(url)
    if base is None:
        return False
    try:
        probe = create_engine(url, isolation_level="AUTOCOMMIT")
        try:
            with probe.connect() as c:
                fresh = bool(
                    c.execute(text("SELECT to_regclass('public.alembic_version') IS NULL")).scalar()
                )
        finally:
            probe.dispose()
        if not fresh:
            return False

        normalized = normalize_database_url(base)
        key = (normalized, revision)
        admin = create_engine(normalized, isolation_level="AUTOCOMMIT")
        try:
            template = _TEMPLATE_CACHE.get(key)
            if template is None:
                template = f"minos_tmpl_{os.getpid()}_{len(_TEMPLATE_CACHE)}"
                with admin.connect() as c:
                    c.execute(text(f'DROP DATABASE IF EXISTS "{template}" WITH (FORCE)'))
                    c.execute(text(f'CREATE DATABASE "{template}"'))
                _alembic_upgrade_real(_swap_db(base, template), revision)
                _TEMPLATE_CACHE[key] = template
            name = make_url(url).database
            with admin.connect() as c:
                # CREATE ... TEMPLATE requires zero connections on the template: defensively
                # terminate any straggler an Alembic engine may have left pooled.
                c.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :t AND pid <> pg_backend_pid()"
                    ),
                    {"t": template},
                )
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
        finally:
            admin.dispose()
        return True
    except Exception:
        return False


def alembic_upgrade(url: str, revision: str = "head") -> None:
    if _clone_from_template(url, revision):
        return
    _alembic_upgrade_real(url, revision)


def _drop_cluster_templates(url: str) -> None:
    """Remove this cluster's cached template databases before a downgrade.

    A downgrade test reasons about the WHOLE cluster (e.g. "roles are fully dropped when nothing
    references them"), and a persistent template database would pin roles and grants the test
    expects gone. Dropping the templates restores the pre-optimization semantics exactly; the
    next fresh upgrade simply rebuilds them once.
    """
    base = _SCRATCH_BASE.get(url)
    if base is None:
        return
    normalized = normalize_database_url(base)
    doomed = [(key, name) for key, name in _TEMPLATE_CACHE.items() if key[0] == normalized]
    if not doomed:
        return
    with contextlib.suppress(Exception):
        admin = create_engine(normalized, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as c:
                for key, name in doomed:
                    c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                    _TEMPLATE_CACHE.pop(key, None)
        finally:
            admin.dispose()


def alembic_downgrade(url: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    _drop_cluster_templates(url)

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
    url = _swap_db(base_url, name)
    _SCRATCH_BASE[url] = base_url
    try:
        yield url
    finally:
        _SCRATCH_BASE.pop(url, None)
        admin2 = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
        with admin2.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin2.dispose()


@pytest.fixture(scope="session")
def isolated_pg_base_url() -> Iterator[str]:
    """A dedicated, isolated bundled PostgreSQL cluster for F3-C1 accepted-path tests.

    The accepted-path persistence entry point requires ``current_database() ==
    minos_engine_db``, so its tests must run against a database literally named
    ``minos_engine_db``. In GitHub Actions the service container's database is ALSO named
    ``minos_engine_db`` (the canonical operational name); creating/dropping a scratch database
    of that name there is impossible — the admin connection is itself attached to
    ``minos_engine_db`` (``cannot drop the currently open database``) — and the contract forbids
    dropping, recreating, migrating or writing the CI service database at all.

    This fixture therefore always spins up a **separate** bundled ``pgserver`` cluster,
    independent of ``MINOS_DATABASE_URL``. Its base URL points at that cluster's own maintenance
    database (``postgres``), so ``scratch_database(isolated_pg_base_url, "minos_engine_db")`` can
    create and drop a throwaway ``minos_engine_db`` inside it without the admin connection ever
    being attached to the database being dropped, and without ever touching the CI service
    database. ``pgserver`` is a hard dev dependency (installed in CI), so this never skips there.
    """
    try:
        import pgserver
    except Exception:  # pragma: no cover - pgserver is a dev dependency, present in CI
        pytest.skip("pgserver is required for the isolated accepted-path cluster")
    tmp = tempfile.mkdtemp(prefix="minos_l2f_iso_")
    server = pgserver.get_server(tmp)
    try:
        _tune_ephemeral_cluster(server.get_uri())
        yield server.get_uri()
    finally:
        with contextlib.suppress(Exception):
            server.cleanup()


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
