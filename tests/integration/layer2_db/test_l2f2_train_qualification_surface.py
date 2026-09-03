"""The ephemeral TRAIN observation surface: identity, privilege, and refusal.

The surface is the only thing standing between a least-privilege evaluator and the closed TRAIN
ledger, so the tests that matter are the ones that install something *almost* right and require a
refusal: same name with a different body, right body with the wrong owner, an argument, or an
EXECUTE grant to anyone else.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.qualification.l2f2_train_qualification_surface import (
    TRAIN_SURFACE_FUNCTION,
    TrainQualificationSurfaceError,
    drop_train_qualification_surface,
    install_train_qualification_surface,
    observe,
    snapshot_train_state,
    surface_body_sha256,
    verify_train_qualification_surface,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.test_l2f_plan_store import _engine

_DB = "minos_l2f2_baseline"
_TRAIN_REVISION = "0020_l2f2_phase_c_execution"


@pytest.fixture
def train_store(isolated_pg_base_url: str) -> Any:
    """A scratch store at the closed TRAIN revision."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _TRAIN_REVISION)
        engine = _engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def test_the_surface_installs_and_authenticates(train_store: Any) -> None:
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
    with train_store.connect() as conn:
        identity = verify_train_qualification_surface(conn)
    assert identity["owner"] == "minos_admin"
    assert identity["security_definer"] is True
    assert identity["volatility"] == "STABLE"
    assert identity["arguments"] == 0
    assert identity["body_sha256"] == surface_body_sha256()
    assert "minos_evaluator=X/" in identity["acl"]


def test_installation_changes_no_scientific_state(train_store: Any) -> None:
    with train_store.connect() as conn, conn.begin():
        before = snapshot_train_state(conn)
        after = install_train_qualification_surface(conn)
    assert before == after
    with train_store.connect() as conn, conn.begin():
        dropped = drop_train_qualification_surface(conn)
    assert dropped == before


def test_the_surface_refuses_the_wrong_database(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, "minos_l2f2_validation") as url:
        alembic_upgrade(url, "0026_l2f2_phase_d_closure")
        engine = _engine(url)
        try:
            with (
                engine.connect() as conn,
                conn.begin(),
                pytest.raises(TrainQualificationSurfaceError, match="refusing to install"),
            ):
                install_train_qualification_surface(conn)
        finally:
            engine.dispose()


def test_the_surface_refuses_a_wrong_revision(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, "0019_l2f2_phase_b_bootstrap")
        engine = _engine(url)
        try:
            with (
                engine.connect() as conn,
                conn.begin(),
                pytest.raises(TrainQualificationSurfaceError, match="revision"),
            ):
                install_train_qualification_surface(conn)
        finally:
            engine.dispose()


def test_a_missing_surface_is_refused(train_store: Any) -> None:
    with (
        train_store.connect() as conn,
        pytest.raises(TrainQualificationSurfaceError, match="found 0"),
    ):
        verify_train_qualification_surface(conn)


def test_a_function_with_the_right_name_but_a_different_body_is_refused(
    train_store: Any,
) -> None:
    """The decisive identity test. A name is not a function."""
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
        conn.execute(text(f"DROP FUNCTION {TRAIN_SURFACE_FUNCTION}()"))
        conn.execute(text("SET ROLE minos_admin"))
        conn.execute(
            text(
                f"CREATE FUNCTION {TRAIN_SURFACE_FUNCTION}() RETURNS jsonb "
                "LANGUAGE plpgsql STABLE SECURITY DEFINER "
                "SET search_path = pg_catalog, public "
                'AS $x$ BEGIN RETURN \'{"schema_version":"forged"}\'::jsonb; END; $x$'
            )
        )
        conn.execute(text("RESET ROLE"))
        conn.execute(
            text(f"GRANT EXECUTE ON FUNCTION {TRAIN_SURFACE_FUNCTION}() TO minos_evaluator")
        )
    with (
        train_store.connect() as conn,
        pytest.raises(TrainQualificationSurfaceError, match="different function"),
    ):
        verify_train_qualification_surface(conn)


def test_a_surface_owned_by_the_wrong_role_is_refused(train_store: Any) -> None:
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
        conn.execute(text(f"ALTER FUNCTION {TRAIN_SURFACE_FUNCTION}() OWNER TO postgres"))
    with (
        train_store.connect() as conn,
        pytest.raises(TrainQualificationSurfaceError, match="owned by"),
    ):
        verify_train_qualification_surface(conn)


def test_a_surface_that_is_not_security_definer_is_refused(train_store: Any) -> None:
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
        conn.execute(text(f"ALTER FUNCTION {TRAIN_SURFACE_FUNCTION}() SECURITY INVOKER"))
    with (
        train_store.connect() as conn,
        pytest.raises(TrainQualificationSurfaceError, match="SECURITY DEFINER"),
    ):
        verify_train_qualification_surface(conn)


def test_a_volatile_surface_is_refused(train_store: Any) -> None:
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
        conn.execute(text(f"ALTER FUNCTION {TRAIN_SURFACE_FUNCTION}() VOLATILE"))
    with (
        train_store.connect() as conn,
        pytest.raises(TrainQualificationSurfaceError, match="volatility"),
    ):
        verify_train_qualification_surface(conn)


@pytest.mark.parametrize("role", ["PUBLIC", "minos_runner", "minos_trainer", "minos_live"])
def test_an_extra_execute_grantee_is_refused(train_store: Any, role: str) -> None:
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION {TRAIN_SURFACE_FUNCTION}() TO {role}"))
    with train_store.connect() as conn, pytest.raises(TrainQualificationSurfaceError):
        verify_train_qualification_surface(conn)


def test_a_surface_taking_an_argument_is_refused(train_store: Any) -> None:
    """A caller-steerable observation surface is not an observation surface."""
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
        conn.execute(text(f"DROP FUNCTION {TRAIN_SURFACE_FUNCTION}()"))
        conn.execute(text("SET ROLE minos_admin"))
        conn.execute(
            text(
                f"CREATE FUNCTION {TRAIN_SURFACE_FUNCTION}(which text) RETURNS jsonb "
                "LANGUAGE plpgsql STABLE SECURITY DEFINER "
                "SET search_path = pg_catalog, public "
                "AS $x$ BEGIN RETURN '{}'::jsonb; END; $x$"
            )
        )
        conn.execute(text("RESET ROLE"))
    with (
        train_store.connect() as conn,
        pytest.raises(TrainQualificationSurfaceError, match="arguments"),
    ):
        verify_train_qualification_surface(conn)


def test_the_surface_refuses_when_the_frozen_plans_are_absent(train_store: Any) -> None:
    """A bare 0020 store carries no campaign, and the surface says so rather than returning zeros.

    This is the shape that matters: an empty result would be an internally consistent summary of
    nothing, and would have hashed cleanly into a qualification.
    """
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
    with train_store.connect() as conn, pytest.raises(Exception, match="three frozen TRAIN plans"):
        observe(conn)


def test_the_surface_exposes_no_truth_config_or_test_content() -> None:
    """Read the source-controlled body: it must reach none of the forbidden surfaces."""
    from minos_engine.qualification import l2f2_train_qualification_surface as mod

    body = mod._body()
    for forbidden in (
        "truth",
        "mutations",
        "config_payload",
        "feature",
        "bam_profile",
        "dataset_evaluation_identity",
        "'test'",
        "'validation'",
    ):
        assert forbidden not in body, forbidden
