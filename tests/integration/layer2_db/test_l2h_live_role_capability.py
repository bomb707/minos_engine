"""What the LIVE ROLE can do, executed as the live role against real PostgreSQL 16.

``r0001`` granted ``minos_live`` SELECT on ``profiling.bam_profiles`` and
``catalog.dataset_registry`` and the persistence qualification certified that as least privilege
on the strength of a SQL recorder showing the code never scanned them. That is a fact about the
code. Least privilege is a fact about the role, and with those grants
``SET ROLE minos_live; SELECT * FROM profiling.bam_profiles`` returned the profile identity of
every partition in the store, TEST included.

These tests therefore never inspect the code. They assume the live role on a real connection and
require PostgreSQL itself to refuse, with SQLSTATE ``42501`` -- an empty result or a constraint
failure would prove nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.runtime_decision_contract import (
    LIVE_PROFILE_RESOLVER,
    LIVE_PROFILE_RESOLVER_SIGNATURE,
    R0002_REVISION,
    REQUIRED_MAIN_REVISION,
    RUNTIME_OVERLAY_REVISION,
)
from minos_engine.storage.runtime_overlay import upgrade_runtime_overlay

from .conftest import alembic_upgrade, scratch_database

INSUFFICIENT_PRIVILEGE = "42501"
NULL_VALUE_NOT_ALLOWED = "22004"


def _sqlstate(error: DBAPIError) -> str:
    return str(getattr(getattr(error, "orig", None), "sqlstate", "unknown"))


def _live_url(url: str) -> str:
    """The same database on a connection that has ASSUMED minos_live."""
    return (
        make_url(normalize_database_url(url))
        .update_query_dict({"options": "-c role=minos_live"})
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="module")
def corrected(pg_base_url: str):
    """A store at the overlay head, and a second engine that connects AS the live role."""
    with scratch_database(pg_base_url, "minos_l2h_capability") as url:
        alembic_upgrade(url, REQUIRED_MAIN_REVISION)
        upgrade_runtime_overlay(url)
        admin = create_engine(normalize_database_url(url))
        live = create_engine(_live_url(url))
        try:
            yield admin, live
        finally:
            live.dispose()
            admin.dispose()


def test_the_connection_really_has_assumed_the_live_role(corrected):
    _admin, live = corrected
    with live.connect() as conn:
        assert str(conn.execute(text("SELECT current_user")).scalar()) == "minos_live"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM profiling.bam_profiles",
        "SELECT count(*) FROM profiling.bam_profiles",
        "SELECT profile_id FROM profiling.bam_profiles LIMIT 1",
        "SELECT EXISTS (SELECT 1 FROM profiling.bam_profiles)",
        "SELECT 1 FROM profiling.bam_profiles WHERE profile_id LIKE '%'",
    ],
)
def test_the_live_role_cannot_enumerate_the_profile_table(corrected, sql: str):
    _admin, live = corrected
    with live.connect() as conn, pytest.raises(DBAPIError) as caught:
        conn.execute(text(sql))
    assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM catalog.dataset_registry",
        "SELECT count(*) FROM catalog.dataset_registry",
        "SELECT round_id FROM catalog.dataset_registry LIMIT 1",
        "SELECT EXISTS (SELECT 1 FROM catalog.dataset_registry)",
    ],
)
def test_the_live_role_cannot_enumerate_the_registry_table(corrected, sql: str):
    _admin, live = corrected
    with live.connect() as conn, pytest.raises(DBAPIError) as caught:
        conn.execute(text(sql))
    assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "relation",
    [
        "profiling.bam_profiles",
        "profiling.profile_snapshot_members",
        "profiling.training_profile_members",
        "catalog.dataset_registry",
        "catalog.split_allocations",
        "evaluation.sealed_test_profile_members",
        "evaluation.validation_profile_members",
    ],
)
def test_the_live_role_holds_no_privilege_on_any_partition_bearing_relation(
    corrected, relation: str
):
    """The sealed relations are probed BY PRIVILEGE. No row is read to prove a row is unreadable."""
    admin, _live = corrected
    with admin.connect() as conn:
        assert (
            conn.execute(
                text("SELECT has_table_privilege('minos_live', :r, 'SELECT')"), {"r": relation}
            ).scalar()
            is False
        ), relation


def test_the_live_role_can_resolve_one_profile_through_the_narrow_surface(corrected):
    _admin, live = corrected
    with live.connect() as conn:
        rows = conn.execute(
            text(f"SELECT * FROM {LIVE_PROFILE_RESOLVER}(:p, :r)"),
            {"p": "a-profile-that-does-not-exist", "r": "a-round"},
        ).all()
    assert rows == [], "an unknown profile resolves to nothing, not to an error"


@pytest.mark.parametrize(
    "arguments", ["NULL, NULL", "'', ''", "NULL, 'a-round'", "'a-profile', ''"]
)
def test_the_resolver_refuses_absent_arguments_rather_than_matching_everything(
    corrected, arguments: str
):
    _admin, live = corrected
    with live.connect() as conn, pytest.raises(DBAPIError) as caught:
        conn.execute(text(f"SELECT * FROM {LIVE_PROFILE_RESOLVER}({arguments})"))
    assert _sqlstate(caught.value) == NULL_VALUE_NOT_ALLOWED


def test_public_cannot_execute_the_resolver(corrected):
    admin, _live = corrected
    with admin.connect() as conn:
        assert (
            conn.execute(
                text("SELECT has_function_privilege('public', :f, 'EXECUTE')"),
                {"f": LIVE_PROFILE_RESOLVER_SIGNATURE},
            ).scalar()
            is False
        )
        assert (
            conn.execute(
                text("SELECT has_function_privilege('minos_live', :f, 'EXECUTE')"),
                {"f": LIVE_PROFILE_RESOLVER_SIGNATURE},
            ).scalar()
            is True
        )


@pytest.mark.parametrize("role", ["minos_runner", "minos_evaluator", "minos_trainer"])
def test_no_other_application_role_can_execute_the_resolver(corrected, role: str):
    admin, _live = corrected
    with admin.connect() as conn:
        assert (
            conn.execute(
                text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                {"r": role, "f": LIVE_PROFILE_RESOLVER_SIGNATURE},
            ).scalar()
            is False
        )
    with admin.connect() as conn:
        savepoint = conn.begin_nested()
        try:
            conn.execute(text(f"SET ROLE {role}"))
            with pytest.raises(DBAPIError) as caught:
                conn.execute(text(f"SELECT * FROM {LIVE_PROFILE_RESOLVER}('a', 'b')"))
            assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE
        finally:
            savepoint.rollback()


def test_the_resolver_is_a_hardened_security_definer(corrected):
    admin, _live = corrected
    with admin.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT pg_get_userbyid(p.proowner) AS owner, p.prosecdef, "
                    "p.proconfig::text AS config, l.lanname, p.prosrc "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "JOIN pg_language l ON l.oid = p.prolang "
                    "WHERE n.nspname = 'runtime' AND p.proname = 'l2h_resolve_owned_profile'"
                )
            )
            .mappings()
            .first()
        )
        assert row is not None
        assert row["owner"] == "minos_admin"
        assert row["prosecdef"] is True
        assert "search_path=pg_catalog, pg_temp" in str(row["config"])
        assert row["lanname"] == "plpgsql"
        body = str(row["prosrc"]).upper()
        for token in ("EXECUTE ", "FORMAT(", "QUOTE_IDENT", "||"):
            assert token not in body, f"no dynamic SQL: {token}"
        assert "CARDINALITY_VIOLATION" in body, "more than one row must raise, not return"
        assert "NULL_VALUE_NOT_ALLOWED" in body, "absent arguments must raise"
        assert (
            conn.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = 'minos_admin'")
            ).scalar()
            is False
        ), "a definer function must not run as a superuser"


def test_exactly_one_security_definer_function_exists(corrected):
    admin, _live = corrected
    with admin.connect() as conn:
        names = sorted(
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT n.nspname || '.' || p.proname FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prosecdef "
                    "AND n.nspname IN ('catalog','profiling','experiments','evaluation',"
                    "'models','runtime','audit')"
                )
            )
        )
    assert names == [LIVE_PROFILE_RESOLVER]


def test_the_live_role_can_still_read_the_two_schema_version_tables(corrected):
    """One revision string each: no identity, no partition, nothing sealed."""
    _admin, live = corrected
    with live.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM public.alembic_version")).scalar()
            == REQUIRED_MAIN_REVISION
        )
        assert (
            conn.execute(text("SELECT version_num FROM runtime.alembic_version_runtime")).scalar()
            == R0002_REVISION
        )


def test_r0001_alone_would_still_be_vulnerable(pg_base_url: str):
    """The defect is real, not hypothetical: at r0001 the live role can list every identity."""
    with scratch_database(pg_base_url, "minos_l2h_r0001_only") as url:
        alembic_upgrade(url, REQUIRED_MAIN_REVISION)
        upgrade_runtime_overlay(url, RUNTIME_OVERLAY_REVISION)
        live = create_engine(_live_url(url))
        try:
            with live.connect() as conn:
                assert (
                    conn.execute(text("SELECT count(*) FROM profiling.bam_profiles")).scalar()
                    is not None
                ), "r0001 grants the capability this corrective removes"
        finally:
            live.dispose()
