"""Real PostgreSQL fixtures for DB-V2 D2 (migration 0009).

Every fixture here uses a **dedicated bundled cluster**. Migration 0009 preflights nine cluster
roles with an exact LOGIN/NOLOGIN configuration, and four of those names already exist as NOLOGIN
in any cluster that has run V1's ``0001``; provisioning them the way an operational step would
must therefore never touch a cluster another suite shares. The operational store is never opened.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from minos_engine.storage.database import normalize_database_url

_ENV = "MINOS_DATABASE_URL"

#: exactly the configuration migration 0009 preflights. 0009 creates, alters and drops none of it.
ROLE_CONFIGURATION: dict[str, str] = {
    "minos_migrate": "LOGIN",
    "minos_owner": "NOLOGIN",
    "minos_planner": "LOGIN",
    "minos_enqueue": "LOGIN",
    "minos_runner": "LOGIN",
    "minos_verifier": "LOGIN",
    "minos_trainer": "LOGIN",
    "minos_evaluator": "LOGIN",
    "minos_live": "LOGIN",
}


@pytest.fixture(scope="session")
def dbv2_cluster_url() -> Iterator[str]:
    """A dedicated bundled cluster whose roles this suite is allowed to provision."""
    try:
        import pgserver
    except Exception:  # pragma: no cover - pgserver is a dev dependency, present in CI
        pytest.skip("pgserver is required for the DB-V2 D2 cluster")
    tmp = tempfile.mkdtemp(prefix="minos_dbv2_")
    server = pgserver.get_server(tmp)
    try:
        yield server.get_uri()
    finally:
        with contextlib.suppress(Exception):
            server.cleanup()


def provision_roles(base_url: str, configuration: dict[str, str] | None = None) -> None:
    """What an operational provisioning step does BEFORE the migration runs. 0009 never does this."""
    roles = ROLE_CONFIGURATION if configuration is None else configuration
    admin = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            for role, login in roles.items():
                conn.execute(
                    text(
                        "DO $$ BEGIN "
                        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
                        f"CREATE ROLE {role} {login}; ELSE ALTER ROLE {role} {login}; "
                        "END IF; END $$;"
                    )
                )
            if "minos_owner" in roles and "minos_migrate" in roles:
                conn.execute(text("GRANT minos_owner TO minos_migrate"))
    finally:
        admin.dispose()


@contextlib.contextmanager
def dbv2_scratch_database(base_url: str, name: str) -> Iterator[str]:
    """A fresh database whose CREATE privilege the definer principal holds, dropped afterwards."""
    admin = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            conn.execute(text(f'GRANT CREATE ON DATABASE "{name}" TO minos_owner'))
    finally:
        admin.dispose()
    url = (
        make_url(normalize_database_url(base_url))
        .set(database=name)
        .render_as_string(hide_password=False)
    )
    try:
        yield url
    finally:
        admin2 = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
        with admin2.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin2.dispose()


def alembic_upgrade(url: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get(_ENV)
    os.environ[_ENV] = url
    try:
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        if previous is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = previous


def alembic_downgrade(url: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get(_ENV)
    os.environ[_ENV] = url
    try:
        command.downgrade(Config("alembic.ini"), revision)
    finally:
        if previous is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = previous


def rows(url: str, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            return [tuple(row) for row in conn.execute(text(sql), params)]
    finally:
        engine.dispose()


def scalar(url: str, sql: str, **params: Any) -> Any:
    return rows(url, sql, **params)[0][0]


def revision_of(url: str) -> str:
    return str(scalar(url, "SELECT version_num FROM alembic_version"))


@pytest.fixture(scope="session")
def dbv2_url(dbv2_cluster_url: str) -> Iterator[str]:
    """A scratch database at 0009, shared read-mostly by the introspection suites."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_head") as url:
        alembic_upgrade(url, "0009_dbv2_shadow_schema")
        yield url


#: the eight schemas that must be byte-for-byte unaffected by 0009.
V1_SCHEMAS = (
    "catalog",
    "profiling",
    "experiments",
    "evaluation",
    "models",
    "runtime",
    "audit",
    "public",
)

_FINGERPRINT_QUERIES: dict[str, str] = {
    "schemas": ("SELECT nspname FROM pg_namespace WHERE nspname = ANY(:s) ORDER BY 1"),
    "relations": (
        "SELECT n.nspname, c.relname, c.relkind, c.relpersistence, c.relrowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ANY(:s) AND c.relkind IN ('r','v','m','S') ORDER BY 1, 2"
    ),
    "columns": (
        "SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
        "       a.attnotnull, pg_get_expr(d.adbin, d.adrelid) "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE n.nspname = ANY(:s) AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY 1, 2, a.attnum"
    ),
    "constraints": (
        "SELECT n.nspname, c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid) "
        "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ANY(:s) ORDER BY 1, 2, 3"
    ),
    "indexes": (
        "SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = ANY(:s) ORDER BY 1, 2, 3"
    ),
    "triggers": (
        "SELECT n.nspname, c.relname, t.tgname, t.tgtype, p.proname, "
        "       pg_get_triggerdef(t.oid) "
        "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE n.nspname = ANY(:s) AND NOT t.tgisinternal ORDER BY 1, 2, 3"
    ),
    "functions": (
        "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), p.prosecdef, "
        "       COALESCE(array_to_string(p.proconfig, ','), ''), md5(p.prosrc) "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = ANY(:s) ORDER BY 1, 2, 3"
    ),
    "table_grants": (
        "SELECT n.nspname, c.relname, COALESCE(array_to_string(c.relacl::text[], '|'), '') "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ANY(:s) AND c.relkind IN ('r','v','S') ORDER BY 1, 2"
    ),
    "schema_grants": (
        "SELECT nspname, COALESCE(array_to_string(nspacl::text[], '|'), '') "
        "FROM pg_namespace WHERE nspname = ANY(:s) ORDER BY 1"
    ),
    "function_grants": (
        "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), "
        "       COALESCE(array_to_string(p.proacl::text[], '|'), '') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = ANY(:s) ORDER BY 1, 2, 3"
    ),
    "table_settings": (
        "SELECT n.nspname, c.relname, COALESCE(array_to_string(c.reloptions, ','), '') "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ANY(:s) AND c.relkind = 'r' ORDER BY 1, 2"
    ),
    "database_grants": (
        "SELECT datname, COALESCE(array_to_string(datacl::text[], '|'), '') "
        "FROM pg_database WHERE datname = current_database()"
    ),
}


def fingerprint(url: str, schemas: tuple[str, ...] = V1_SCHEMAS) -> dict[str, Any]:
    """Every catalog fact about the V1 schemas, plus row counts and deterministic row hashes."""
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            captured: dict[str, Any] = {
                name: [
                    tuple(str(v) for v in row)
                    for row in conn.execute(text(sql), {"s": list(schemas)})
                ]
                for name, sql in _FINGERPRINT_QUERIES.items()
            }
            counts: dict[str, int] = {}
            hashes: dict[str, str] = {}
            for schema, table, kind, *_ in captured["relations"]:
                if kind != "r":
                    continue
                ident = f'"{schema}"."{table}"'
                counts[f"{schema}.{table}"] = int(
                    conn.execute(text(f"SELECT count(*) FROM {ident}")).scalar_one()
                )
                if counts[f"{schema}.{table}"]:
                    hashes[f"{schema}.{table}"] = str(
                        conn.execute(
                            text(
                                "SELECT md5(string_agg(t.row_text, E'\\n' ORDER BY t.row_text)) "
                                f"FROM (SELECT r::text AS row_text FROM {ident} AS r) AS t"
                            )
                        ).scalar_one()
                    )
            captured["row_counts"] = counts
            captured["row_hashes"] = hashes
            return captured
    finally:
        engine.dispose()


@pytest.fixture
def isolated_cluster_url() -> Iterator[str]:
    """A throwaway cluster for the preflight-failure tests, which must drop a required role."""
    try:
        import pgserver
    except Exception:  # pragma: no cover - pgserver is a dev dependency, present in CI
        pytest.skip("pgserver is required for the DB-V2 preflight-failure cluster")
    tmp = tempfile.mkdtemp(prefix="minos_dbv2_iso_")
    server = pgserver.get_server(tmp)
    try:
        yield server.get_uri()
    finally:
        with contextlib.suppress(Exception):
            server.cleanup()


@pytest.fixture
def dbv2_fresh_url(dbv2_cluster_url: str, request: pytest.FixtureRequest) -> Iterator[str]:
    """A scratch database of its own, at 0009, for tests that must COMMIT.

    The shared ``dbv2_url`` database is kept pristine by rolling every test back. A concurrency
    test cannot do that - both callers have to commit for the race to be real - so those tests get
    a database nobody else observes.
    """
    provision_roles(dbv2_cluster_url)
    name = f"minos_dbv2_{abs(hash(request.node.nodeid)) % 10**9}"
    with dbv2_scratch_database(dbv2_cluster_url, name) as url:
        alembic_upgrade(url, "0009_dbv2_shadow_schema")
        yield url
