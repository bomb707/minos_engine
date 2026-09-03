"""0026: the closure read surface, and the seal it completes.

The lifecycle is the point. A store at 0026 has finished executing and finished evaluating, and
both older boundaries must say so out loud rather than quietly still working:

    0024 EXECUTE  ->  0025 EVALUATE  ->  0026 CLOSE
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.test_l2f_plan_store import _engine

_DB = "minos_l2f2_validation"
_PRIOR = "0025_l2f2_phase_d_eval_auth"
_HEAD = "0026_l2f2_phase_d_closure"
_VIEW = "evaluation.l2f_phase_d_closure_inputs"
_EXPECTED_COLUMNS = [
    "plan_hash",
    "job_id",
    "job_key",
    "job_status",
    "member_index",
    "config_index",
    "config_hash",
    "dataset_id",
    "round_id",
    "chromosome",
    "execution_result_id",
    "execution_result_hash",
    "execution_runtime_ms",
    "execution_environment_hash",
    "execution_failure_id",
    "execution_failure_code",
    "execution_failure_runtime_ms",
    "execution_failure_environment_hash",
    "evaluation_id",
    "evaluation_hash",
    # ONE canonical contract column: it names the contract of THIS terminal outcome, success or
    # failure, so no reader ever has to guess which contract a column belongs to.
    "evaluation_scoring_contract_hash",
    "minos_score",
    "admitted",
    "admission_code",
    "evaluation_failure_id",
    "evaluation_failure_code",
]


def _columns(conn: Any) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                " WHERE table_schema='evaluation' AND table_name='l2f_phase_d_closure_inputs' "
                " ORDER BY ordinal_position"
            )
        )
    ]


def _structure(conn: Any) -> list[tuple[Any, ...]]:
    """Everything 0026 could have disturbed: tables, columns, constraints, views, grants."""
    return [
        tuple(r)
        for r in conn.execute(
            text(
                "SELECT table_schema, table_name, column_name, data_type, is_nullable "
                "  FROM information_schema.columns "
                " WHERE table_schema IN ('experiments','evaluation','catalog','profiling') "
                "   AND table_name <> 'l2f_phase_d_closure_inputs' "
                " ORDER BY 1,2,3"
            )
        )
    ]


def test_this_migration_is_the_head_and_the_name_fits() -> None:
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[3] / "migrations" / "versions"
    revisions: dict[str, str | None] = {}
    for path in sorted(versions.glob("*.py")):
        body = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)', body, re.M)
        down = re.search(r'^down_revision(?::[^=]+)?\s*=\s*["\']?([^"\'\n]+)', body, re.M)
        if rev:
            value = down.group(1).strip() if down else None
            revisions[rev.group(1)] = None if value in (None, "None") else value
    children = {d for d in revisions.values() if d}
    assert [r for r in revisions if r not in children] == [_HEAD]
    assert revisions[_HEAD] == _PRIOR
    assert len(_HEAD) <= 32, "alembic_version.version_num is varchar(32)"


def test_migrations_0001_through_0025_are_untouched() -> None:
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    changed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", "HEAD", "--", "migrations/versions/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert [c for c in changed if "0026" not in c] == []


def test_the_closure_view_exposes_exactly_the_agreed_columns(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                assert _columns(conn) == _EXPECTED_COLUMNS
        finally:
            engine.dispose()


def test_the_closure_view_discloses_no_truth_config_or_feature(isolated_pg_base_url: str) -> None:
    """Closure ranks decided outcomes; it never re-derives one, so it gets none of the inputs."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                columns = " ".join(_columns(conn)).lower()
                definition = str(
                    conn.execute(
                        text(
                            "SELECT pg_get_viewdef('evaluation.l2f_phase_d_closure_inputs'::regclass)"
                        )
                    ).scalar_one()
                ).lower()
        finally:
            engine.dispose()
    for leaked in (
        "truth",
        "mutation",
        "config_payload",
        "payload",
        "bam",
        "feature",
        "matrix",
        "profile",
        "vcf",
        "uri",
        "path",
    ):
        assert leaked not in columns, leaked
    for leaked in (
        "truth_vcf",
        "mutations_vcf",
        "dataset_evaluation_identity",
        "feature_matrix",
        "bam_profiles",
        "config_payload_id",
    ):
        assert leaked not in definition, leaked


def test_the_closure_view_is_validation_only_by_construction(isolated_pg_base_url: str) -> None:
    """TRAIN and TEST are not rows a caller could filter badly; there is no partition parameter."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                definition = str(
                    conn.execute(
                        text(
                            "SELECT pg_get_viewdef('evaluation.l2f_phase_d_closure_inputs'::regclass)"
                        )
                    ).scalar_one()
                ).lower()
        finally:
            engine.dispose()
    assert "'validation'" in definition
    assert "'train'" not in definition
    assert "'test'" not in definition
    assert "$1" not in definition and "$2" not in definition, "the view takes a parameter"


def test_the_view_owner_is_the_non_superuser_control_plane(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                owner = conn.execute(
                    text(
                        "SELECT viewowner FROM pg_views WHERE schemaname='evaluation' "
                        "  AND viewname='l2f_phase_d_closure_inputs'"
                    )
                ).scalar_one()
                assert owner == "minos_admin"
                attrs = conn.execute(
                    text(
                        "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolbypassrls "
                        "  FROM pg_roles WHERE rolname='minos_admin'"
                    )
                ).one()
                assert not any(attrs), attrs
        finally:
            engine.dispose()


def test_only_the_evaluator_may_read_and_nobody_may_write(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for role, expected in (
                    ("minos_evaluator", True),
                    ("minos_runner", False),
                    ("minos_trainer", False),
                    ("minos_live", False),
                    ("public", False),
                ):
                    got = conn.execute(
                        text("SELECT has_table_privilege(:r, :v, 'SELECT')"),
                        {"r": role, "v": _VIEW},
                    ).scalar_one()
                    assert got is expected, (role, got)
                for role in (
                    "minos_evaluator",
                    "minos_runner",
                    "minos_trainer",
                    "minos_live",
                    "public",
                ):
                    for privilege in ("INSERT", "UPDATE", "DELETE"):
                        assert (
                            conn.execute(
                                text("SELECT has_table_privilege(:r, :v, :p)"),
                                {"r": role, "v": _VIEW, "p": privilege},
                            ).scalar_one()
                            is False
                        ), (role, privilege)
        finally:
            engine.dispose()


def test_the_evaluator_still_has_no_direct_access_to_the_experiments_tables(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for table in (
                    "experiments.l2f_experiment_plans",
                    "experiments.l2f_experiment_jobs",
                    "experiments.l2f_execution_results",
                    "experiments.l2f_execution_failures",
                ):
                    assert (
                        conn.execute(
                            text("SELECT has_table_privilege('minos_evaluator', :t, 'SELECT')"),
                            {"t": table},
                        ).scalar_one()
                        is False
                    ), table
        finally:
            engine.dispose()


def test_no_security_definer_function_was_added(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                before = {
                    r[0]
                    for r in conn.execute(
                        text("SELECT proname FROM pg_proc WHERE prosecdef ORDER BY 1")
                    )
                }
            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                after = {
                    r[0]
                    for r in conn.execute(
                        text("SELECT proname FROM pg_proc WHERE prosecdef ORDER BY 1")
                    )
                }
            assert after == before, after - before
        finally:
            engine.dispose()


def test_the_migration_round_trips_without_disturbing_anything_else(
    isolated_pg_base_url: str,
) -> None:
    """§31 — 0025 -> 0026 -> 0025 -> 0026, with the rest of the schema byte-for-byte stable."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                baseline = _structure(conn)
                assert (
                    conn.execute(text("SELECT to_regclass(:v)"), {"v": _VIEW}).scalar_one() is None
                )

            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                assert conn.execute(text("SELECT to_regclass(:v)"), {"v": _VIEW}).scalar_one()
                assert _structure(conn) == baseline
                first_acl = conn.execute(
                    text("SELECT relacl::text FROM pg_class WHERE oid = CAST(:v AS regclass)"),
                    {"v": _VIEW},
                ).scalar_one()

            alembic_downgrade(url, _PRIOR)
            with engine.connect() as conn:
                assert (
                    conn.execute(text("SELECT to_regclass(:v)"), {"v": _VIEW}).scalar_one() is None
                )
                assert _structure(conn) == baseline

            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                assert _structure(conn) == baseline
                assert (
                    conn.execute(
                        text("SELECT relacl::text FROM pg_class WHERE oid = CAST(:v AS regclass)"),
                        {"v": _VIEW},
                    ).scalar_one()
                    == first_acl
                ), "the ACL drifted across a re-upgrade"
                assert (
                    conn.execute(
                        text(
                            "SELECT viewowner FROM pg_views WHERE viewname="
                            "'l2f_phase_d_closure_inputs'"
                        )
                    ).scalar_one()
                    == "minos_admin"
                )
        finally:
            engine.dispose()


# --------------------------------------------------------------------------------------------
# §30 — the lifecycle seal, asserted at every revision
# --------------------------------------------------------------------------------------------
def _pins() -> dict[str, str]:
    from minos_engine.evaluation.phase_d_closure_service import PHASE_D_CLOSURE_REVISION
    from minos_engine.evaluation.phase_d_service import PHASE_D_EVALUATOR_REVISION
    from minos_engine.storage.l2f2_runner import VALIDATION_REVISION

    return {
        "runner": VALIDATION_REVISION,
        "evaluator": PHASE_D_EVALUATOR_REVISION,
        "closer": PHASE_D_CLOSURE_REVISION,
    }


def test_the_three_boundaries_pin_three_consecutive_revisions() -> None:
    """Older pins must NOT be bumped: each one going stale is what seals its phase."""
    assert _pins() == {
        "runner": "0024_l2f2_phase_d_anchor",
        "evaluator": "0025_l2f2_phase_d_eval_auth",
        "closer": "0026_l2f2_phase_d_closure",
    }


@pytest.mark.parametrize(
    ("revision", "runner_ok", "evaluator_ok", "closer_ok"),
    [
        pytest.param("0024_l2f2_phase_d_anchor", True, False, False, id="0024-execute"),
        pytest.param("0025_l2f2_phase_d_eval_auth", False, True, False, id="0025-evaluate"),
        pytest.param("0026_l2f2_phase_d_closure", False, False, True, id="0026-close"),
    ],
)
def test_each_revision_admits_exactly_one_boundary(
    isolated_pg_base_url: str, revision: str, runner_ok: bool, evaluator_ok: bool, closer_ok: bool
) -> None:
    """The state machine, proved rather than asserted in prose. No GATK, no evaluation."""
    from minos_engine.evaluation.phase_d_closure_service import (
        authorize_validation_closure_connection,
    )
    from minos_engine.evaluation.phase_d_service import authorize_validation_evaluator_connection
    from minos_engine.storage.l2f2_runner import authorize_validation_runner_connection

    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, revision)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                live = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                assert live == revision
                for label, fn, expected in (
                    ("runner", authorize_validation_runner_connection, runner_ok),
                    ("evaluator", authorize_validation_evaluator_connection, evaluator_ok),
                    ("closer", authorize_validation_closure_connection, closer_ok),
                ):
                    try:
                        fn(conn)
                        admitted = True
                        reason = ""
                    except Exception as exc:
                        admitted = False
                        reason = str(exc)
                    if expected:
                        # the principal here is the migration login, so a revision-satisfied
                        # boundary may still refuse on PRINCIPAL grounds -- never on revision.
                        assert "revision" not in reason.lower(), (label, reason)
                    else:
                        assert not admitted, f"{label} was admitted at {revision}"
                        assert "revision" in reason.lower(), (label, reason)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------------------------
# CORRECTIVE: foreign scoring contracts must not cross-contaminate
#
# The evaluation exclusivity invariant is on (execution_result_id, scoring_contract_hash), NOT on
# execution_result_id alone. Independently joining the success and failure ledgers therefore
# paired an accepted success with a foreign failure on one row -- and a reader preferring the
# failure would let a foreign contract overwrite this campaign's result.
#
# These assert the ACTUAL SQL the view emits, not hand-built Python dictionaries.
# --------------------------------------------------------------------------------------------
_ACCEPTED_CONTRACT = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
_FOREIGN_CONTRACT = "f" * 64


def _view_rows(conn: Any, execution_result_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            text(
                "SELECT evaluation_scoring_contract_hash, evaluation_id, evaluation_failure_id, "
                "       minos_score, admitted, evaluation_failure_code "
                f"  FROM {_VIEW} WHERE execution_result_id = :e "
                " ORDER BY evaluation_scoring_contract_hash"
            ),
            {"e": execution_result_id},
        ).mappings()
    ]


@pytest.fixture
def crossed(isolated_pg_base_url: str, tmp_path: Any) -> Any:
    """A scratch 0026 store with ONE execution, ready to receive terminal outcomes."""
    from minos_engine.baseline.finalist_freeze import load_finalist_freeze
    from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
    from minos_engine.storage.l2f2_validation_prepare import (
        ACCEPTED_FINALIST_FREEZE_SHA256,
        ACCEPTED_PHASE_C_CLOSURE_SHA256,
    )
    from tests.integration.layer2_db.l2f2_phase_d_closure_seed import seed_single_execution
    from tests.l2f2_phase_d_fixture import FIXTURE_FREEZE_PATH

    authority = build_l2f2_phase_d_authority(
        load_finalist_freeze(
            FIXTURE_FREEZE_PATH,
            expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
            expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
        )
    )
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            execution_id, registry_id = seed_single_execution(engine, authority, tmp_path)
            yield engine, execution_id, registry_id
        finally:
            engine.dispose()


def _add_success(
    engine: Any, execution_id: str, registry_id: str, *, contract: str, score: float
) -> None:
    from tests.integration.layer2_db.l2f2_phase_d_closure_seed import add_evaluation_success

    add_evaluation_success(engine, execution_id, registry_id, contract=contract, score=score)


def _add_failure(
    engine: Any, execution_id: str, registry_id: str, *, contract: str, code: str = "HAPPY_TIMEOUT"
) -> None:
    from tests.integration.layer2_db.l2f2_phase_d_closure_seed import add_evaluation_failure

    add_evaluation_failure(engine, execution_id, registry_id, contract=contract, code=code)


def test_case_a_accepted_success_beside_a_foreign_failure(crossed: Any) -> None:
    """The pairing that previously let a foreign failure overwrite an accepted success."""
    engine, execution_id, registry_id = crossed
    _add_success(engine, execution_id, registry_id, contract=_ACCEPTED_CONTRACT, score=0.75)
    _add_failure(engine, execution_id, registry_id, contract=_FOREIGN_CONTRACT)

    with engine.connect() as conn:
        rows = _view_rows(conn, execution_id)
    assert len(rows) == 2, rows
    accepted = [r for r in rows if r["evaluation_scoring_contract_hash"] == _ACCEPTED_CONTRACT]
    foreign = [r for r in rows if r["evaluation_scoring_contract_hash"] == _FOREIGN_CONTRACT]
    assert len(accepted) == 1 and len(foreign) == 1
    # the accepted row is a pure success; no foreign failure leaked onto it
    assert accepted[0]["evaluation_id"] is not None
    assert accepted[0]["evaluation_failure_id"] is None
    assert accepted[0]["evaluation_failure_code"] is None
    assert float(accepted[0]["minos_score"]) == pytest.approx(0.75)
    assert accepted[0]["admitted"] is True
    # and the foreign row is a pure failure carrying no score
    assert foreign[0]["evaluation_id"] is None
    assert foreign[0]["minos_score"] is None
    assert foreign[0]["evaluation_failure_id"] is not None


def test_case_b_foreign_success_beside_an_accepted_failure(crossed: Any) -> None:
    """The inverse pairing: a foreign success must not mask this campaign's failure."""
    engine, execution_id, registry_id = crossed
    _add_success(engine, execution_id, registry_id, contract=_FOREIGN_CONTRACT, score=0.99)
    _add_failure(engine, execution_id, registry_id, contract=_ACCEPTED_CONTRACT)

    with engine.connect() as conn:
        rows = _view_rows(conn, execution_id)
    assert len(rows) == 2, rows
    accepted = next(r for r in rows if r["evaluation_scoring_contract_hash"] == _ACCEPTED_CONTRACT)
    assert accepted["evaluation_failure_id"] is not None
    assert accepted["evaluation_id"] is None
    assert accepted["minos_score"] is None, "a foreign success leaked a score onto our failure"


def test_case_c_an_accepted_success_alone(crossed: Any) -> None:
    engine, execution_id, registry_id = crossed
    _add_success(engine, execution_id, registry_id, contract=_ACCEPTED_CONTRACT, score=0.5)
    with engine.connect() as conn:
        rows = _view_rows(conn, execution_id)
    assert len(rows) == 1
    assert rows[0]["evaluation_scoring_contract_hash"] == _ACCEPTED_CONTRACT
    assert rows[0]["evaluation_failure_id"] is None


def test_case_d_an_accepted_failure_alone(crossed: Any) -> None:
    engine, execution_id, registry_id = crossed
    _add_failure(engine, execution_id, registry_id, contract=_ACCEPTED_CONTRACT)
    with engine.connect() as conn:
        rows = _view_rows(conn, execution_id)
    assert len(rows) == 1
    assert rows[0]["evaluation_id"] is None
    assert rows[0]["evaluation_failure_id"] is not None


def test_case_e_a_foreign_outcome_alone_leaves_the_pair_undecided(crossed: Any) -> None:
    """A foreign terminal outcome is not evidence: the frozen pair is simply not decided."""
    from minos_engine.baseline.finalist_freeze import load_finalist_freeze
    from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
    from minos_engine.baseline.phase_d_observations import (
        PhaseDClosureError,
        derive_phase_d_observations,
    )
    from minos_engine.storage.l2f2_validation_prepare import (
        ACCEPTED_FINALIST_FREEZE_SHA256,
        ACCEPTED_PHASE_C_CLOSURE_SHA256,
    )
    from tests.l2f2_phase_d_fixture import FIXTURE_FREEZE_PATH

    engine, execution_id, registry_id = crossed
    _add_success(engine, execution_id, registry_id, contract=_FOREIGN_CONTRACT, score=0.99)

    authority = build_l2f2_phase_d_authority(
        load_finalist_freeze(
            FIXTURE_FREEZE_PATH,
            expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
            expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
        )
    )
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(f"SELECT * FROM {_VIEW}")).mappings()]
    assert len(rows) == 1
    assert rows[0]["evaluation_scoring_contract_hash"] == _FOREIGN_CONTRACT
    with pytest.raises(PhaseDClosureError, match="frozen cross product"):
        derive_phase_d_observations(rows, authority=authority)


def test_no_row_ever_carries_both_a_success_and_a_failure(crossed: Any) -> None:
    """The structural guarantee the UNION provides: exactly one side is populated per row."""
    engine, execution_id, registry_id = crossed
    _add_success(engine, execution_id, registry_id, contract=_ACCEPTED_CONTRACT, score=0.5)
    _add_failure(engine, execution_id, registry_id, contract=_FOREIGN_CONTRACT)
    with engine.connect() as conn:
        both = conn.execute(
            text(
                f"SELECT count(*) FROM {_VIEW} "
                " WHERE evaluation_id IS NOT NULL AND evaluation_failure_id IS NOT NULL"
            )
        ).scalar_one()
    assert both == 0
