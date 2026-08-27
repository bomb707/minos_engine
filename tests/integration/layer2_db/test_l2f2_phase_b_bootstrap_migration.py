"""Migration ``0019`` — the runner learns which Phase-B plan it may consume, without any science.

The first real Phase-B invocation failed before it claimed anything, and it failed for the right
reason: the entry opened the store as the runner and then built a ``PhaseBAuthority``, whose
derivation reads the completed Phase-A SCIENTIFIC ledger. The runner is denied that on purpose —
it is truth-free by construction — so the fix cannot be a grant. It is a narrow function that
answers the only two questions a worker needs: which plan, and under which runtime.

Everything the answer depends on is checked inside the database, and the call takes no arguments,
so there is no parameter through which a worker could point itself somewhere else.

No GATK: executions come from ``FakeGatkRunner`` through the private test seam.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f2_runner_boundary import l2f2 as _l2f2_fixture
from tests.integration.layer2_db.test_l2f2_runner_boundary import service as _service_fixture
from tests.integration.layer2_db.test_l2f_plan_store import _engine

l2f2 = _l2f2_fixture
service = _service_fixture

_DB = "minos_l2f2_baseline"
_PRIOR = "0018_l2f2_eval_owner_fix"
_HEAD = "0019_l2f2_phase_b_bootstrap"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

_BOOTSTRAP = "experiments.l2f2_resolve_phase_b_runner_bootstrap()"
_BOOTSTRAP_SQL = (
    "SELECT plan_hash, execution_environment_hash FROM "
    "experiments.l2f2_resolve_phase_b_runner_bootstrap()"
)


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _function(engine: Any) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT p.oid::text AS oid, pg_get_userbyid(p.proowner) AS owner, "
                    "       r.rolsuper AS owner_superuser, p.prosecdef, p.provolatile::text AS vol, "
                    "       p.proconfig::text AS config, pg_get_functiondef(p.oid) AS definition "
                    "  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    " WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": _BOOTSTRAP},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# the migration itself
# --------------------------------------------------------------------------- #
def test_lifecycle_0018_0019_0018_0019_adds_exactly_one_function(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0018 = _state(engine)
            assert _function(engine) is None
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            fn = _function(engine)
            assert fn is not None
            assert fn["owner"] == "minos_admin", "a SECURITY DEFINER function runs as its OWNER"
            assert fn["owner_superuser"] is False
            assert fn["prosecdef"] is True
            assert fn["vol"] == "s", "the bootstrap only reads; it must be STABLE"
            assert "search_path" in str(fn["config"])
            at_0019 = _state(engine)
        finally:
            engine.dispose()

        # no relation, constraint, index, trigger, role, membership or ACL moves at all.
        for section in (
            "relations",
            "constraints",
            "indexes",
            "triggers",
            "roles",
            "role_memberships",
            "schema_security",
            "default_acls",
        ):
            assert json.dumps(at_0018.get(section), sort_keys=True, default=str) == json.dumps(
                at_0019.get(section), sort_keys=True, default=str
            ), f"0019 altered {section!r}"

        def _by_name(state: Any) -> dict[str, Any]:
            return {f"{r['schema']}.{r['name']}": r for r in state["functions"]}

        added = sorted(set(_by_name(at_0019)) - set(_by_name(at_0018)))
        assert added == ["experiments.l2f2_resolve_phase_b_runner_bootstrap"]
        assert not (set(_by_name(at_0018)) - set(_by_name(at_0019))), "0019 removed a function"
        changed = sorted(
            key
            for key in set(_by_name(at_0018)) & set(_by_name(at_0019))
            if json.dumps(_by_name(at_0018)[key], sort_keys=True, default=str)
            != json.dumps(_by_name(at_0019)[key], sort_keys=True, default=str)
        )
        assert changed == [], f"0019 redefined {changed}"

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _function(engine) is None
            assert _state(engine) == at_0018, "downgrade did not restore 0018"
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _function(engine) is not None
        finally:
            engine.dispose()


def test_the_bootstrap_reads_nothing_in_the_evaluation_schema(isolated_pg_base_url: str) -> None:
    """THE point of the whole corrective, asserted against the function's own definition.

    A truth-free worker may execute this. If its body could reach an evaluation table, executing
    it would hand that worker the answer key by proxy — the definer runs as the control plane.
    """
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = str(_function(engine)["definition"])  # type: ignore[index]
            assert "evaluation." not in definition
            for forbidden in (
                "l2f_evaluation_results",
                "l2f_evaluation_failures",
                "dataset_evaluation_identity",
                "minos_score",
                "admission",
                "truth",
                "metrics",
            ):
                assert forbidden not in definition, f"the bootstrap reads {forbidden!r}"
            # it reads exactly the execution-side relations it needs, and nothing else.
            for expected in (
                "l2f2_execution_authorities",
                "l2f_experiment_plans",
                "l2f_experiment_jobs",
                "l2f_execution_results",
                "l2f_execution_failures",
            ):
                assert expected in definition
        finally:
            engine.dispose()


def test_only_the_runner_and_the_control_plane_may_execute_it(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                execute = {
                    role: bool(
                        conn.execute(
                            text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                            {"r": role, "f": _BOOTSTRAP},
                        ).scalar_one()
                    )
                    for role in (*_ROLES, "public")
                }
                tables = {
                    table: bool(
                        conn.execute(
                            text("SELECT has_table_privilege('minos_runner', :t, 'SELECT')"),
                            {"t": table},
                        ).scalar_one()
                    )
                    for table in (
                        "experiments.l2f2_execution_authorities",
                        "experiments.l2f_experiment_plans",
                        "experiments.l2f_experiment_jobs",
                        "experiments.l2f_execution_results",
                        "experiments.l2f_execution_failures",
                    )
                }
                evaluation_usage = bool(
                    conn.execute(
                        text("SELECT has_schema_privilege('minos_runner', 'evaluation', 'USAGE')")
                    ).scalar_one()
                )
        finally:
            engine.dispose()

    assert execute == {
        "minos_runner": True,
        "minos_admin": True,
        "minos_evaluator": False,
        "minos_trainer": False,
        "minos_live": False,
        "public": False,
    }
    # the runner gained EXECUTE on one function and not one byte of table access.
    assert tables == dict.fromkeys(tables, False)
    assert evaluation_usage is False, "the runner must stay out of the evaluation schema"


# --------------------------------------------------------------------------- #
# what the bootstrap refuses. Everything is checked inside; nothing is passed in.
# --------------------------------------------------------------------------- #

_PROTOCOL = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"


def _admin(engine: Any, sql: str, **params: Any) -> Any:
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        return conn.execute(text(sql), params)


def _clone_plan(engine: Any, *, plan_hash: str, members: int, candidates: int) -> None:
    """A plan row with a chosen SHAPE, reusing the fixture's upstream rows.

    The bootstrap reads the plan row's own columns, so a shaped row is exactly the fixture this
    needs; no member or config rows are involved in any check it performs. Only
    ``experiment_parameter_policy_hash`` is varied — the one column in the plan's logical-identity
    UNIQUE that no foreign key and no bootstrap check reads — so each clone is a distinct plan.
    """
    _admin(
        engine,
        "INSERT INTO experiments.l2f_experiment_plans ("
        "  profile_snapshot_id, train_feature_matrix_id, feature_set_id, partition, "
        "  snapshot_hash, split_manifest_hash, registry_snapshot_hash, train_matrix_hash, "
        "  train_feature_view_hash, feature_set_hash, feature_registry_hash, gatk_registry_hash, "
        "  parameter_space_hash, experiment_parameter_policy_hash, candidate_set_hash, "
        "  train_member_count, candidate_count, logical_job_count, plan_hash) "
        "SELECT p.profile_snapshot_id, p.train_feature_matrix_id, p.feature_set_id, p.partition, "
        "       p.snapshot_hash, p.split_manifest_hash, p.registry_snapshot_hash, "
        "       p.train_matrix_hash, p.train_feature_view_hash, p.feature_set_hash, "
        "       p.feature_registry_hash, p.gatk_registry_hash, p.parameter_space_hash, "
        "       :plan_hash, p.candidate_set_hash, "
        "       :members, :candidates, :jobs, :plan_hash "
        "  FROM experiments.l2f_experiment_plans p LIMIT 1",
        plan_hash=plan_hash,
        members=members,
        candidates=candidates,
        jobs=members * candidates,
    )


def _phase_b_authority(
    engine: Any, *, plan_hash: str, members: int, candidates: int, jobs: int | None = None
) -> None:
    _admin(
        engine,
        "INSERT INTO experiments.l2f2_execution_authorities ("
        "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
        "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
        "  logical_job_count) "
        "SELECT :proto, 'PHASE_B', p.id, p.plan_hash, a.train_schedule_sha256, "
        "       p.candidate_set_hash, p.parameter_space_hash, :members, :candidates, :jobs "
        "  FROM experiments.l2f_experiment_plans p "
        "  CROSS JOIN (SELECT train_schedule_sha256 FROM experiments.l2f2_execution_authorities "
        "               WHERE phase='PHASE_A' LIMIT 1) a "
        " WHERE p.plan_hash = :plan_hash",
        proto=_PROTOCOL,
        plan_hash=plan_hash,
        members=members,
        candidates=candidates,
        jobs=jobs if jobs is not None else members * candidates,
    )


def _bootstrap(engine: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(_BOOTSTRAP_SQL)).mappings().one()


def test_with_no_phase_b_authority_the_runner_learns_nothing(l2f2: Any) -> None:
    """A store where Phase B was never authorized answers no question at all."""
    from sqlalchemy.exc import DatabaseError

    with pytest.raises(DatabaseError, match="no PHASE_B execution authority"):
        _bootstrap(l2f2.engine)


def test_an_authority_that_disagrees_with_its_plan_is_refused(l2f2: Any) -> None:
    """The authority and the plan must be the same object seen twice, not two opinions."""
    from sqlalchemy.exc import DatabaseError

    _clone_plan(l2f2.engine, plan_hash="b" * 64, members=10, candidates=48)
    # counts that do not match the plan row this authority binds
    _phase_b_authority(l2f2.engine, plan_hash="b" * 64, members=9, candidates=48, jobs=432)

    with pytest.raises(DatabaseError, match="disagrees with its persisted plan"):
        _bootstrap(l2f2.engine)


def test_an_authority_that_is_not_the_frozen_screen_is_refused(l2f2: Any) -> None:
    """Consistent with its plan is not enough; it must be the frozen 10 x 48 = 480 screen."""
    from sqlalchemy.exc import DatabaseError

    _clone_plan(l2f2.engine, plan_hash="c" * 64, members=5, candidates=39)
    _phase_b_authority(l2f2.engine, plan_hash="c" * 64, members=5, candidates=39)

    with pytest.raises(DatabaseError, match="not the frozen 10 x 48 = 480 screen"):
        _bootstrap(l2f2.engine)


def test_two_phase_b_authorities_are_refused_rather_than_one_being_chosen(l2f2: Any) -> None:
    """One plan cannot hold two (the schema forbids it); two plans can, and that is ambiguous."""
    from sqlalchemy.exc import DatabaseError, IntegrityError

    _clone_plan(l2f2.engine, plan_hash="d" * 64, members=10, candidates=48)
    _phase_b_authority(l2f2.engine, plan_hash="d" * 64, members=10, candidates=48)

    # the same plan twice is structurally impossible ...
    with pytest.raises(IntegrityError, match="uq_l2f2_authority_plan"):
        _phase_b_authority(l2f2.engine, plan_hash="d" * 64, members=10, candidates=48)

    # ... a second PLAN is not, and the bootstrap must refuse to pick between them.
    _clone_plan(l2f2.engine, plan_hash="e" * 64, members=10, candidates=48)
    _phase_b_authority(l2f2.engine, plan_hash="e" * 64, members=10, candidates=48)
    with pytest.raises(DatabaseError, match="more than one PHASE_B execution authority"):
        _bootstrap(l2f2.engine)


def test_an_incomplete_phase_a_campaign_yields_no_runtime(l2f2: Any) -> None:
    """Runtime lineage comes from a FINISHED campaign; a partial one has no single answer.

    The fixture's Phase-A authority declares more logical jobs than the store holds, which is
    exactly the shape of a campaign still in flight.
    """
    from sqlalchemy.exc import DatabaseError

    _clone_plan(l2f2.engine, plan_hash="f" * 64, members=10, candidates=48)
    _phase_b_authority(l2f2.engine, plan_hash="f" * 64, members=10, candidates=48)

    with pytest.raises(DatabaseError, match="logical jobs; its runtime lineage is incomplete"):
        _bootstrap(l2f2.engine)


def _drive_phase_a(l2f2: Any, service: Any, *, tail_environment: Any = None) -> None:
    """Enqueue and execute the fixture's WHOLE Phase-A plan, so its campaign is complete.

    ``tail_environment`` runs the last job under a different runtime, which is the only way to
    produce a Phase-A campaign that cannot say which JVM produced it.
    """
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job
    from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
    from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust
    from tests.integration.layer2_db.l2f2_phase_a_env import TEST_EXECUTION_ENVIRONMENT
    from tests.integration.layer2_db.test_l2f_plan_store import _CS

    total = int(l2f2.count("SELECT logical_job_count FROM experiments.l2f_experiment_plans"))
    enqueued = int(l2f2.count("SELECT count(*) FROM experiments.l2f_experiment_jobs"))
    start = enqueued
    while start < total:
        count = min(64, total - start)
        _enqueue_experiment_jobs_with_trust(l2f2.engine, l2f2.plan, _CS, start=start, count=count)
        start += count
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_experiment_jobs") == total

    done = 0
    while done < total:
        remaining = total - done
        # a handful of legitimate candidate failures: a failure carries a runtime identity too.
        runner = FakeGatkRunner(exit_code=2) if done % 37 == 36 else FakeGatkRunner()
        environment = (
            tail_environment
            if (tail_environment is not None and remaining == 1)
            else TEST_EXECUTION_ENVIRONMENT
        )
        dispatched = _execute_l2f2_job(
            l2f2.service if service is None else service,
            l2f2.authority,
            worker_id=f"ci-phase-a-{done % 64}",
            runner=runner,
            dataset_root=l2f2.dataset_root,
            publisher=l2f2.publisher,
            work_root=l2f2.work_root,
            execution_environment=environment,
        )
        assert dispatched is not None
        done += 1


def test_a_complete_phase_a_with_failures_yields_its_single_runtime(
    service: Any, l2f2: Any
) -> None:
    """The positive: a finished campaign, candidate failures included, answers with one runtime.

    Phase A is not required to have succeeded everywhere — the real one has five execution
    failures — because a failure records which runtime produced it exactly as a success does.
    What matters is that every job is terminal and every outcome names the same environment.
    """
    from tests.integration.layer2_db.l2f2_phase_a_env import TEST_EXECUTION_ENVIRONMENT

    _drive_phase_a(l2f2, service)
    failures = int(l2f2.count("SELECT count(*) FROM experiments.l2f_execution_failures"))
    assert failures > 0, "this fixture must include legitimate candidate failures"

    _clone_plan(l2f2.engine, plan_hash="a" * 64, members=10, candidates=48)
    _phase_b_authority(l2f2.engine, plan_hash="a" * 64, members=10, candidates=48)

    ticket = _bootstrap(l2f2.engine)
    assert ticket["plan_hash"] == "a" * 64
    assert ticket["execution_environment_hash"] == TEST_EXECUTION_ENVIRONMENT.environment_hash()


def test_a_phase_a_campaign_with_two_runtimes_yields_no_runtime(service: Any, l2f2: Any) -> None:
    """Two JVMs, two answers, no ticket.

    Phase B explores a design chosen from one runtime's numbers. A campaign that ran under two of
    them cannot say which one that was, so the bootstrap hands out nothing rather than guessing.
    """
    from sqlalchemy.exc import DatabaseError

    from minos_engine.experiments.execution_environment import GatkExecutionEnvironment
    from tests.integration.layer2_db.l2f2_phase_a_env import TEST_EXECUTION_ENVIRONMENT

    other = GatkExecutionEnvironment(
        **{**TEST_EXECUTION_ENVIRONMENT.model_dump(), "java_sha256": "9" * 64}
    )
    assert other.environment_hash() != TEST_EXECUTION_ENVIRONMENT.environment_hash()

    _drive_phase_a(l2f2, service, tail_environment=other)
    _clone_plan(l2f2.engine, plan_hash="a" * 64, members=10, candidates=48)
    _phase_b_authority(l2f2.engine, plan_hash="a" * 64, members=10, candidates=48)

    with pytest.raises(DatabaseError, match="distinct execution environments"):
        _bootstrap(l2f2.engine)
