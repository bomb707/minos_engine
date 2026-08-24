"""Real PostgreSQL 16 fixtures for L2-B integration tests.

Uses ``MINOS_DATABASE_URL`` when set (GitHub CI service container) and otherwise an
ephemeral bundled PostgreSQL 16 server (``pgserver``). Tests skip locally only when
neither is available; in CI they must run (enforced by ``test_ci_guard``). SQLite is
never used.
"""

from __future__ import annotations

import atexit
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


def database_mode() -> str:
    """ "shared" when tests target a persistent MINOS_DATABASE_URL cluster, else "ephemeral"."""
    return "shared" if os.environ.get(_ENV) else "ephemeral"


def _require_xdist_isolation() -> None:
    """Fail closed when xdist workers would share one PostgreSQL cluster.

    Under xdist every worker inherits the same ``MINOS_DATABASE_URL``, and the suite uses fixed
    scratch names with ``DROP DATABASE ... WITH (FORCE)`` — workers would destroy each other's
    databases mid-test. Isolation per worker exists ONLY in ephemeral mode, where each worker
    process provisions its own bundled pgserver cluster.
    """
    if os.environ.get("PYTEST_XDIST_WORKER") and os.environ.get(_ENV):
        pytest.fail(
            "Parallel PostgreSQL integration tests cannot run against a shared "
            f"MINOS_DATABASE_URL cluster (mode={database_mode()}): xdist workers would "
            "FORCE-drop each other's scratch databases. Unset MINOS_DATABASE_URL so each "
            "worker gets its own ephemeral pgserver cluster "
            "(env -u MINOS_DATABASE_URL pytest -n auto --dist loadscope ...), "
            "or run the suite serially.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def pg_base_url() -> Iterator[str]:
    url = os.environ.get(_ENV)
    if url:
        _require_xdist_isolation()
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

#: (normalized base URL, CONCRETE revision) -> template database name, built ONCE per session by
#: running the REAL migration chain, then cloned per test via CREATE DATABASE ... TEMPLATE
#: (~100ms). The key is never the symbolic string "head": it is resolved to the concrete
#: revision first, so "head" and its explicit revision share one template, and a future head
#: automatically gets a different identity.
_TEMPLATE_CACHE: dict[tuple[str, str], str] = {}


def _resolve_revision(revision: str) -> str | None:
    """Resolve an Alembic target to its CONCRETE revision, or ``None`` when in doubt.

    ``None`` makes the template fast path decline and fall open to real Alembic — a symbolic or
    ambiguous target (multiple heads) must never become a cache identity.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config("alembic.ini"))
        if revision == "head":
            heads = script.get_heads()
            if len(heads) != 1:
                return None
            return str(heads[0])
        resolved = script.get_revision(revision)
        if resolved is None:
            return None
        return str(resolved.revision)
    except Exception:
        return None


def _drop_templates(items: list[tuple[tuple[str, str], str]]) -> None:
    """Best-effort drop of OWNED template databases only; never anything else.

    Each entry came from ``_TEMPLATE_CACHE`` and therefore names a database THIS process
    created; the name-prefix assertion is belt-and-braces so no refactor can ever point this
    at a production database. A failure on one template never hides the caller's outcome.
    """
    for key, name in items:
        if not name.startswith("minos_tmpl_"):  # pragma: no cover - structural guard
            continue
        with contextlib.suppress(Exception):
            admin = create_engine(key[0], isolation_level="AUTOCOMMIT")
            try:
                with admin.connect() as c:
                    c.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :t AND pid <> pg_backend_pid()"
                        ),
                        {"t": name},
                    )
                    c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            finally:
                admin.dispose()
        _TEMPLATE_CACHE.pop(key, None)


def drop_session_templates() -> None:
    """Drop every template database THIS process created and clear the cache. Idempotent.

    Without this, a persistent/shared cluster (``MINOS_DATABASE_URL``) would accumulate
    ``minos_tmpl_*`` databases across pytest runs: the in-process cache dies with the process,
    but the physical databases would not — pinning MINOS roles and confusing later runs.
    """
    _drop_templates(list(_TEMPLATE_CACHE.items()))


#: last-resort cleanup even for abnormal exits and runs where the session fixture is not
#: active (a sibling test directory importing these helpers). Idempotent: after the session
#: fixture has cleaned up, the cache is empty and this is a no-op.
atexit.register(drop_session_templates)


@pytest.fixture(scope="session", autouse=True)
def _template_session_cleanup() -> Iterator[None]:
    """Templates never survive the pytest session — pass or fail."""
    yield
    with contextlib.suppress(Exception):
        drop_session_templates()


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

        concrete = _resolve_revision(revision)
        if concrete is None:
            return False
        normalized = normalize_database_url(base)
        key = (normalized, concrete)
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
    _drop_templates([(k, n) for k, n in _TEMPLATE_CACHE.items() if k[0] == normalized])


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
