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


def test_the_surface_refuses_when_the_campaign_authorities_are_absent(
    train_store: Any,
) -> None:
    """A bare 0020 store carries no campaign, and the surface says so rather than returning zeros.

    This is the shape that matters: an empty result would be an internally consistent summary of
    nothing and would have hashed cleanly into a qualification. It also proves the campaigns are
    derived through l2f2_execution_authorities rather than from whatever plan rows happen to exist.
    """
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
    with (
        train_store.connect() as conn,
        pytest.raises(Exception, match="authority-bound TRAIN campaigns"),
    ):
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


# --------------------------------------------------------------------------------------------
# authority binding and the environment union
#
# These are the two defects this corrective exists for. The campaign must come from
# l2f2_execution_authorities, and the environment must come from BOTH terminal ledgers.
# --------------------------------------------------------------------------------------------
_PROTOCOL = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
_ENV = "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"


def test_the_body_derives_campaigns_through_the_execution_authority() -> None:
    """Not from plan rows that merely carry the right hashes."""
    from minos_engine.qualification import l2f2_train_qualification_surface as mod

    body = mod._body()
    assert "experiments.l2f2_execution_authorities" in body
    assert "'PHASE_A'" in body and "'PHASE_B'" in body and "'PHASE_C'" in body
    assert _PROTOCOL in body
    # exact shape agreement between authority and persisted plan
    for agreement in (
        "a.member_count = p.train_member_count",
        "a.candidate_count = p.candidate_count",
        "a.logical_job_count = p.logical_job_count",
        "a.plan_hash = p.plan_hash",
    ):
        assert agreement in body, agreement
    # an unexpected fourth authority or plan is refused
    assert "unexpected execution authority" in body
    assert "unexpected TRAIN plan" in body


def test_the_body_takes_the_environment_from_both_terminal_ledgers() -> None:
    """0015 stores it on results AND failures; reading only results leaves 35 unchecked."""
    from minos_engine.qualification import l2f2_train_qualification_surface as mod

    body = mod._body()
    environment_block = body[body.index("'execution_environment_outcome_count'") :]
    assert "experiments.l2f_execution_results" in environment_block
    assert "experiments.l2f_execution_failures" in environment_block
    assert "execution_environment_null_count" in body


def test_every_fact_is_scoped_to_the_authorized_plans() -> None:
    """Whole-database counts would absorb any row appearing beside the campaign."""
    from minos_engine.qualification import l2f2_train_qualification_surface as mod

    body = mod._body()
    # each scientific aggregate references the authorised plan id array
    assert body.count("v_plan_ids") >= 12
    # succeeded_without_evaluation must be contract-scoped, not merely "has any evaluation"
    swe = body[body.index("'succeeded_without_evaluation'") :]
    swe = swe[: swe.index("'evaluation_count'")]
    assert "scoring_contract_hash = " in swe


def test_a_foreign_contract_evaluation_does_not_satisfy_the_requirement() -> None:
    """An evaluation under some other contract must leave the execution unevaluated."""
    from minos_engine.qualification import l2f2_train_qualification_surface as mod

    swe = mod._body()
    swe = swe[swe.index("'succeeded_without_evaluation'") :]
    swe = swe[: swe.index("'evaluation_count'")]
    assert "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6" in swe


def test_the_provisioner_refuses_a_preexisting_alembic_grant(train_store: Any) -> None:
    """§9 -- it must never revoke a privilege it did not create."""
    with train_store.connect() as conn, conn.begin():
        conn.execute(text("GRANT SELECT ON public.alembic_version TO minos_admin"))
    try:
        with (
            train_store.connect() as conn,
            conn.begin(),
            pytest.raises(TrainQualificationSurfaceError, match="already holds SELECT"),
        ):
            install_train_qualification_surface(conn)
    finally:
        with train_store.connect() as conn, conn.begin():
            conn.execute(text("REVOKE SELECT ON public.alembic_version FROM minos_admin"))


def test_the_alembic_grant_is_taken_and_returned(train_store: Any) -> None:
    with train_store.connect() as conn:
        before = conn.execute(
            text("SELECT has_table_privilege('minos_admin','public.alembic_version','SELECT')")
        ).scalar_one()
    assert before is False
    with train_store.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
    with train_store.connect() as conn:
        assert (
            conn.execute(
                text("SELECT has_table_privilege('minos_admin','public.alembic_version','SELECT')")
            ).scalar_one()
            is True
        )
    with train_store.connect() as conn, conn.begin():
        drop_train_qualification_surface(conn)
    with train_store.connect() as conn:
        assert (
            conn.execute(
                text("SELECT has_table_privilege('minos_admin','public.alembic_version','SELECT')")
            ).scalar_one()
            is False
        ), "the temporary grant outlived the surface"


# --------------------------------------------------------------------------------------------
# candidate-set / parameter-space agreement and EXACT per-phase shapes
#
# 195 + 480 + 500 = 1175 also holds for shapes that are wrong phase by phase, so the aggregate
# is not a substitute for asserting each one.
# --------------------------------------------------------------------------------------------
def _observe(engine: Any) -> dict:
    with engine.connect() as conn, conn.begin():
        install_train_qualification_surface(conn)
    with engine.connect() as conn:
        return observe(conn)


def test_a_correctly_seeded_authority_set_is_accepted(train_store: Any) -> None:
    from tests.integration.layer2_db.l2f2_train_authority_seed import (
        FROZEN_SHAPES,
        seed_train_authorities,
    )

    seed_train_authorities(train_store)
    observed = _observe(train_store)
    assert observed["authority_count"] == 3
    shapes = observed["phase_shapes"]
    for phase, (plan_hash, members, candidates, logical) in FROZEN_SHAPES.items():
        assert observed["phase_plan_map"][phase] == plan_hash
        assert shapes[phase]["members"] == members
        assert shapes[phase]["candidates"] == candidates
        assert shapes[phase]["logical_jobs"] == logical
        assert shapes[phase]["parameter_space_hash"] == (
            "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
        )


@pytest.mark.parametrize(
    ("authority_overrides", "plan_overrides", "label"),
    [
        pytest.param(
            {"PHASE_A": {"candidate_set_hash": "a" * 64}},
            {},
            "candidate-set disagreement",
            id="candidate-set",
        ),
        pytest.param(
            {"PHASE_B": {"parameter_space_hash": "b" * 64}},
            {},
            "parameter-space disagreement",
            id="parameter-space",
        ),
    ],
)
def test_an_authority_disagreeing_with_its_plan_is_refused(
    train_store: Any, authority_overrides: dict, plan_overrides: dict, label: str
) -> None:
    """§2 -- 0020 binds both hashes into the authority; the surface must require them too."""
    from tests.integration.layer2_db.l2f2_train_authority_seed import seed_train_authorities

    seed_train_authorities(
        train_store, authority_overrides=authority_overrides, plan_overrides=plan_overrides
    )
    with pytest.raises(Exception, match="authority-bound TRAIN campaigns"):
        _observe(train_store)


@pytest.mark.parametrize(
    ("phase", "members", "candidates", "logical"),
    [
        pytest.param("PHASE_A", 6, 39, 234, id="phase-a-members"),
        pytest.param("PHASE_B", 10, 47, 470, id="phase-b-candidates"),
        pytest.param("PHASE_C", 50, 9, 450, id="phase-c-candidates"),
    ],
)
def test_a_wrong_phase_shape_is_refused_even_when_the_plan_agrees(
    train_store: Any, phase: str, members: int, candidates: int, logical: int
) -> None:
    """Both sides moved together, so pairwise agreement holds -- the FROZEN shape does not.

    ``ck_l2f_plans_job_count_consistent`` forces ``logical = members * candidates``, so these are
    internally VALID plans. Nothing but the frozen per-phase shape can reject them.
    """
    from tests.integration.layer2_db.l2f2_train_authority_seed import seed_train_authorities

    shape = {"member_count": members, "candidate_count": candidates, "logical_job_count": logical}
    plan_shape = {
        "train_member_count": members,
        "candidate_count": candidates,
        "logical_job_count": logical,
    }
    seed_train_authorities(
        train_store,
        authority_overrides={phase: shape},
        plan_overrides={phase: plan_shape},
    )
    with pytest.raises(Exception, match="authority-bound TRAIN campaigns"):
        _observe(train_store)


def test_shapes_that_still_total_1175_are_refused(train_store: Any) -> None:
    """The decisive one: move work between phases and the aggregate never notices.

    PHASE_B 10x49=490 and PHASE_C 49x10=490 are each internally consistent, and
    195 + 490 + 490 is still 1175. Only the per-phase frozen shapes catch it.
    """
    from tests.integration.layer2_db.l2f2_train_authority_seed import seed_train_authorities

    assert 195 + 490 + 490 == 1175
    b = {"member_count": 10, "candidate_count": 49, "logical_job_count": 490}
    c = {"member_count": 49, "candidate_count": 10, "logical_job_count": 490}
    seed_train_authorities(
        train_store,
        authority_overrides={"PHASE_B": b, "PHASE_C": c},
        plan_overrides={
            "PHASE_B": {"train_member_count": 10, "candidate_count": 49, "logical_job_count": 490},
            "PHASE_C": {"train_member_count": 49, "candidate_count": 10, "logical_job_count": 490},
        },
    )
    with pytest.raises(Exception, match="authority-bound TRAIN campaigns"):
        _observe(train_store)


def test_the_body_requires_both_identity_hashes_and_each_phase_shape() -> None:
    from minos_engine.qualification import l2f2_train_qualification_surface as mod

    body = mod._body()
    assert "a.candidate_set_hash = p.candidate_set_hash" in body
    assert "a.parameter_space_hash = p.parameter_space_hash" in body
    for shape in ("5, 39, 195", "10, 48, 480", "50, 10, 500"):
        assert shape in body, shape
