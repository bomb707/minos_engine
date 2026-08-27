"""The L2-F2 least-privilege runner against a REAL PostgreSQL database at its exact revision.

Everything here runs through an ephemeral ``minos_runner_ci_svc`` LOGIN whose only MINOS
membership is ``minos_runner`` — the exact authority shape the future external service principal
will have. What passes here is therefore what the real service can actually do, and what is
refused here is refused for the right reason: a privilege check, never a malformed statement.

No GATK, no hap.py, no truth, no score. Inputs are synthetic bytes and the runner is
``FakeGatkRunner`` driven through the PRIVATE test seam; the public entry constructs the real
``SubprocessGatkRunner`` and is covered structurally in the unit suite.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from minos_engine.baseline.phase_a import CanaryIdentity, PhaseAAuthority
from minos_engine.baseline.protocol import build_baseline_protocol
from minos_engine.experiments.plan import iter_logical_jobs
from minos_engine.storage.l2f2_runner import (
    BaselineRunnerAuthorityError,
    _execute_l2f2_job,
    authorize_baseline_runner_connection,
)
from minos_engine.storage.l2f_execution_inputs import DatasetRoot
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust
from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust
from minos_engine.storage.l2f_result_publisher import ResultArtifactPublisher
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f2_phase_a_env import TEST_EXECUTION_ENVIRONMENT
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.test_l2f_execution import (
    _prepare_env,
    _result_root,
    _work_root,
)
from tests.integration.layer2_db.test_l2f_plan_store import (
    _CS,
    _SNAPSHOT_A,
    _engine,
    _provisioned_root,
    _publisher,
)

_L2F = "0006_l2f_experiment_plan"
_RUNNER_BOUNDARY = "0011_l2f2_runner_boundary"
#: the EXACT revision the runner boundary requires. It tracks the shared baseline store: 0011
#: introduced this boundary's own functions and grants, 0012 separated the plan's two index
#: namespaces, 0013 let evaluation store what the pinned upstream scorer exposes, 0015 bound the
#: execution environment into every outcome and 0016 admitted Phase B. It fails closed on every
#: revision but the current one — including the one immediately behind it.
_REQUIRED = "0019_l2f2_phase_b_bootstrap"
_BASELINE_DB = "minos_l2f2_baseline"
_CI_ROLE = "minos_runner_ci_svc"
_AUTHORITIES = "experiments.l2f2_execution_authorities"

_DENIED_STATEMENTS = [
    "INSERT INTO catalog.artifacts (uri, sha256) VALUES ('file:///x', repeat('a', 64))",
    "UPDATE catalog.artifacts SET uri = 'file:///y'",
    "DELETE FROM catalog.artifacts",
    "UPDATE experiments.l2f_experiment_jobs SET status = 'PENDING'",
    "DELETE FROM experiments.l2f_experiment_jobs",
    "UPDATE experiments.l2f_execution_results SET result_hash = repeat('c', 64)",
    "DELETE FROM experiments.l2f_execution_results",
    "UPDATE experiments.l2f_experiment_plan_configs SET config_hash = repeat('b', 64)",
    "INSERT INTO experiments.l2f2_execution_authorities (phase) VALUES ('PHASE_A')",
    "DELETE FROM experiments.l2f2_execution_authorities",
]


class _Env:
    """A baseline store at the required revision holding a plan, its authority and jobs."""

    def __init__(
        self,
        url: str,
        engine: Any,
        plan: Any,
        authority: PhaseAAuthority,
        tmp_path: Path,
        dataset_root: Path,
    ) -> None:
        self.url = url
        self.engine = engine
        self.plan = plan
        self.authority = authority
        self.tmp_path = tmp_path
        self.dataset_root = DatasetRoot.from_path(dataset_root)
        self.publisher = ResultArtifactPublisher(_result_root(tmp_path))
        self.work_root = _work_root(tmp_path)

    def count(self, sql: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(sql)).scalar_one())

    def status(self, job_id: str) -> str:
        with self.engine.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT status FROM experiments.l2f_experiment_jobs WHERE id = :i"),
                    {"i": job_id},
                ).scalar_one()
            )


@pytest.fixture
def l2f2(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    """A real baseline database named and versioned exactly as the runner demands."""
    plan, identity, dataset_root = _prepare_env(isolated_pg_base_url, tmp_path, _SNAPSHOT_A, jobs=2)
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _L2F)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan, dataset_identity=identity)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            _enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=0, count=2)
            engine.dispose()
            alembic_upgrade(url, _REQUIRED)
            engine = _engine(url)

            first = next(iter_logical_jobs(plan))
            authority = PhaseAAuthority(
                baseline_protocol_hash=build_baseline_protocol().protocol_hash,
                train_schedule_manifest_sha256="a" * 64,
                split_manifest_sha256="b" * 64,
                plan=plan,
                canary=CanaryIdentity(
                    logical_index=0,
                    job_key=first.job_key,
                    member_index=first.member_index,
                    dataset_id=plan.members[0].dataset_id,
                    round_id="r0",
                    chromosome="chr18",
                    config_index=0,
                    config_hash=plan.configs[0].config_hash,
                ),
            )
            with engine.connect() as conn, conn.begin():
                conn.execute(text("SET LOCAL ROLE minos_admin"))
                conn.execute(
                    text(
                        f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                        "  baseline_protocol_hash, phase, plan_id, plan_hash, "
                        "  train_schedule_sha256, candidate_set_hash, parameter_space_hash, "
                        "  member_count, candidate_count, logical_job_count, canary_job_key) "
                        "SELECT :proto, 'PHASE_A', p.id, p.plan_hash, :sched, :cand, :space, "
                        "       :members, :configs, :jobs, :canary "
                        "  FROM experiments.l2f_experiment_plans p WHERE p.plan_hash = :plan_hash"
                    ),
                    {
                        "proto": authority.baseline_protocol_hash,
                        "sched": "a" * 64,
                        "cand": plan.candidate_set_hash,
                        "space": plan.parameter_space_hash,
                        "members": plan.train_member_count,
                        "configs": plan.candidate_count,
                        "jobs": plan.logical_job_count,
                        "canary": first.job_key,
                        "plan_hash": plan.plan_hash,
                    },
                )
            yield _Env(url, engine, plan, authority, tmp_path, dataset_root)
        finally:
            engine.dispose()


def _service_engine(l2f2: Any) -> Any:
    """Create the ephemeral runner principal: minos_runner and nothing else."""
    from sqlalchemy.engine import make_url

    url = make_url(l2f2.url)
    with l2f2.engine.connect() as conn, conn.begin():
        conn.execute(text(f"DROP ROLE IF EXISTS {_CI_ROLE}"))
        conn.execute(
            text(
                f"CREATE ROLE {_CI_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOBYPASSRLS INHERIT"
            )
        )
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{url.database}" TO {_CI_ROLE}'))
        conn.execute(text(f"GRANT minos_runner TO {_CI_ROLE}"))
    return create_engine(url.set(username=_CI_ROLE, password=""))


@pytest.fixture
def service(l2f2: Any) -> Any:
    engine = _service_engine(l2f2)
    try:
        yield engine
    finally:
        engine.dispose()
        from sqlalchemy.engine import make_url

        database = make_url(l2f2.url).database
        with l2f2.engine.connect() as conn, conn.begin():
            conn.execute(text(f'REVOKE ALL ON DATABASE "{database}" FROM {_CI_ROLE}'))
            conn.execute(text(f"REVOKE minos_runner FROM {_CI_ROLE}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {_CI_ROLE}"))


def _good_runner() -> FakeGatkRunner:
    return FakeGatkRunner()


# --------------------------------------------------------------------------- #
# connection authorization
# --------------------------------------------------------------------------- #
def test_the_runner_principal_holds_only_minos_runner(service: Any) -> None:
    with service.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                    "  FROM pg_roles WHERE rolname = session_user"
                )
            )
            .mappings()
            .one()
        )
        assert row["rolcanlogin"] is True
        for attribute in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls"):
            assert row[attribute] is False, attribute
        memberships = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT r.rolname FROM pg_auth_members m "
                    "  JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles g ON g.oid = m.member "
                    " WHERE g.rolname = session_user AND r.rolname LIKE 'minos%'"
                )
            )
        }
        assert memberships == {"minos_runner"}
        authorize_baseline_runner_connection(conn)


def test_an_administrative_connection_is_refused_by_the_runner_boundary(l2f2: Any) -> None:
    """Even a superuser connection is not a runner connection."""
    with l2f2.engine.connect() as conn, pytest.raises(BaselineRunnerAuthorityError):
        authorize_baseline_runner_connection(conn)


def test_the_boundary_refuses_a_database_that_is_not_the_baseline_store(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, "minos_not_the_baseline") as url:
        alembic_upgrade(url, _REQUIRED)
        engine = _engine(url)
        try:
            with (
                engine.connect() as conn,
                pytest.raises(BaselineRunnerAuthorityError, match="refuses database"),
            ):
                authorize_baseline_runner_connection(conn)
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    "revision",
    [
        "0010_l2f2_evaluation_corrective",
        _RUNNER_BOUNDARY,
        "0012_l2f_plan_member_source_idx",
        "0013_l2f2_upstream_score_oracle",
        "0014_l2f2_exec_failure_runtime",
        "0015_l2f2_exec_environment",
        "0016_l2f2_phase_b_execution",
        "0017_l2f2_owner_corrective",
        "0018_l2f2_eval_owner_fix",
    ],
)
def test_the_boundary_refuses_a_database_at_the_wrong_revision(
    isolated_pg_base_url: str,
    revision: str,
) -> None:
    """EXACT revision, both directions of "close enough" — 0011 is refused like 0010 is."""
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, revision)
        engine = _engine(url)
        try:
            with (
                engine.connect() as conn,
                pytest.raises(BaselineRunnerAuthorityError, match="revision"),
            ):
                authorize_baseline_runner_connection(conn)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# the least-privilege permission matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("statement", _DENIED_STATEMENTS)
def test_every_denial_statement_actually_reaches_the_privilege_check(
    l2f2: Any, statement: str
) -> None:
    """A denial proves nothing if the statement dies in the parser first."""
    with pytest.raises(Exception) as excinfo, l2f2.engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(text(statement))
        raise _Executed
    message = str(excinfo.value).lower()
    assert "does not exist" not in message, "the statement names something that does not exist"
    assert "syntax error" not in message, "the statement is malformed"


class _Executed(Exception):
    """Raised to roll back a probe that ran successfully under an authorised role."""


@pytest.mark.parametrize("statement", _DENIED_STATEMENTS)
def test_the_runner_principal_cannot_mutate_any_table(service: Any, statement: str) -> None:
    with pytest.raises(Exception) as excinfo, service.connect() as conn, conn.begin():
        conn.execute(text(statement))
    assert "permission denied" in str(excinfo.value).lower()


@pytest.mark.parametrize("role", ["minos_admin", "minos_evaluator", "minos_trainer", "minos_live"])
def test_the_runner_principal_cannot_assume_another_role(service: Any, role: str) -> None:
    with pytest.raises(Exception) as excinfo, service.connect() as conn, conn.begin():
        conn.execute(text(f"SET ROLE {role}"))
    assert "permission denied" in str(excinfo.value).lower()


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM experiments.l2f_experiment_jobs",
        "SELECT count(*) FROM experiments.l2f_experiment_plans",
        "SELECT count(*) FROM experiments.l2f_execution_results",
        "SELECT count(*) FROM experiments.l2f2_execution_authorities",
        "SELECT count(*) FROM evaluation.dataset_evaluation_identity",
        "SELECT count(*) FROM evaluation.l2f_evaluation_results",
    ],
)
def test_the_runner_principal_cannot_read_ledgers_or_truth_directly(
    service: Any, statement: str
) -> None:
    """The runner reads scientific identity ONLY through the narrow resolve function."""
    with pytest.raises(Exception) as excinfo, service.connect() as conn:
        conn.execute(text(statement))
    assert "permission denied" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# a complete least-privilege execution
# --------------------------------------------------------------------------- #
def test_a_complete_execution_runs_entirely_under_the_runner_principal(
    service: Any, l2f2: Any
) -> None:
    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id="ci-runner-1",
        runner=_good_runner(),
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        execution_environment=TEST_EXECUTION_ENVIRONMENT,
    )

    assert dispatched is not None
    assert dispatched.status == "SUCCEEDED"
    assert dispatched.execution_result_id, "the evaluator's authoritative input must be returned"
    assert dispatched.result_hash and dispatched.vcf_sha256
    assert dispatched.result_manifest_sha256
    assert l2f2.status(dispatched.job_id) == "SUCCEEDED"
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_execution_results") == 1
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_execution_failures") == 0

    with l2f2.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT id FROM experiments.l2f_execution_results WHERE job_id = :j"),
            {"j": dispatched.job_id},
        ).scalar_one()
        assert str(stored) == dispatched.execution_result_id
        artifacts = (
            conn.execute(
                text(
                    "SELECT provenance, media_type FROM catalog.artifacts "
                    " WHERE provenance IN ('l2f:gatk-vcf', 'l2f:execution-result-json') "
                    " ORDER BY provenance"
                )
            )
            .mappings()
            .all()
        )
    assert [a["provenance"] for a in artifacts] == [
        "l2f:execution-result-json",
        "l2f:gatk-vcf",
    ]
    assert [a["media_type"] for a in artifacts] == [
        "application/vnd.minos.l2f-execution-result+json",
        "application/vnd.ga4gh.vcf",
    ]


def test_a_failing_execution_records_exactly_one_bounded_failure(service: Any, l2f2: Any) -> None:
    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id="ci-runner-2",
        runner=FakeGatkRunner(exit_code=4),
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        execution_environment=TEST_EXECUTION_ENVIRONMENT,
    )

    assert dispatched is not None
    assert dispatched.status == "FAILED"
    assert dispatched.failure_code == "GATK_NONZERO_EXIT"
    assert dispatched.execution_result_id is None
    assert l2f2.status(dispatched.job_id) == "FAILED"
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_execution_failures") == 1
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_execution_results") == 0


def test_no_job_is_left_stranded_after_both_outcomes(service: Any, l2f2: Any) -> None:
    for worker, runner in (("w-ok", _good_runner()), ("w-bad", FakeGatkRunner(exit_code=7))):
        _execute_l2f2_job(
            service,
            l2f2.authority,
            worker_id=worker,
            runner=runner,
            dataset_root=l2f2.dataset_root,
            publisher=l2f2.publisher,
            work_root=l2f2.work_root,
            execution_environment=TEST_EXECUTION_ENVIRONMENT,
        )
    assert (
        l2f2.count(
            "SELECT count(*) FROM experiments.l2f_experiment_jobs "
            " WHERE status IN ('CLAIMED', 'RUNNING')"
        )
        == 0
    )
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_execution_results") == 1
    assert l2f2.count("SELECT count(*) FROM experiments.l2f_execution_failures") == 1


def test_an_empty_queue_returns_none(service: Any, l2f2: Any) -> None:
    for worker in ("w-1", "w-2"):
        _execute_l2f2_job(
            service,
            l2f2.authority,
            worker_id=worker,
            runner=_good_runner(),
            dataset_root=l2f2.dataset_root,
            publisher=l2f2.publisher,
            work_root=l2f2.work_root,
            execution_environment=TEST_EXECUTION_ENVIRONMENT,
        )
    assert (
        _execute_l2f2_job(
            service,
            l2f2.authority,
            worker_id="w-3",
            runner=_good_runner(),
            dataset_root=l2f2.dataset_root,
            publisher=l2f2.publisher,
            work_root=l2f2.work_root,
            execution_environment=TEST_EXECUTION_ENVIRONMENT,
        )
        is None
    )


def test_a_plan_without_an_execution_authority_cannot_be_run(service: Any, l2f2: Any) -> None:
    """The authority is what makes a plan an L2-F2 plan; without it the runner refuses."""
    # removing the authority requires the table OWNER and a disabled append-only trigger, which
    # is precisely why no application or control-plane role can do it.
    with l2f2.engine.connect() as conn, conn.begin():
        conn.execute(text(f"ALTER TABLE {_AUTHORITIES} DISABLE TRIGGER USER"))
        conn.execute(text(f"DELETE FROM {_AUTHORITIES}"))  # noqa: S608
        conn.execute(text(f"ALTER TABLE {_AUTHORITIES} ENABLE TRIGGER USER"))

    with pytest.raises(Exception, match="execution authority|not an owned"):
        _execute_l2f2_job(
            service,
            l2f2.authority,
            worker_id="w-no-authority",
            runner=_good_runner(),
            dataset_root=l2f2.dataset_root,
            publisher=l2f2.publisher,
            work_root=l2f2.work_root,
            execution_environment=TEST_EXECUTION_ENVIRONMENT,
        )


def test_the_execution_authority_is_protected_twice(l2f2: Any) -> None:
    """Two independent defences: the control plane has no UPDATE grant, and the trigger refuses."""
    with (
        pytest.raises(Exception, match="permission denied"),
        l2f2.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(text(f"UPDATE {_AUTHORITIES} SET phase = 'PHASE_A'"))  # noqa: S608

    # and even the table owner is refused by the append-only trigger
    with pytest.raises(Exception, match="append-only"), l2f2.engine.connect() as conn, conn.begin():
        conn.execute(text(f"UPDATE {_AUTHORITIES} SET phase = 'PHASE_A'"))  # noqa: S608


def test_the_authority_refuses_a_protocol_other_than_the_frozen_one(l2f2: Any) -> None:
    with (
        pytest.raises(Exception, match="ck_l2f2_authority_frozen_protocol"),
        l2f2.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                "  logical_job_count, canary_job_key) "
                "SELECT repeat('e', 64), 'PHASE_A', p.id, p.plan_hash, repeat('a', 64), "
                "       repeat('b', 64), repeat('c', 64), 1, 1, 1, repeat('d', 64) "
                "  FROM experiments.l2f_experiment_plans p LIMIT 1"
            )
        )


def test_the_registrar_refuses_an_unsupported_artifact_kind(service: Any) -> None:
    with (
        pytest.raises(Exception, match="unsupported execution artifact kind"),
        service.connect() as conn,
        conn.begin(),
    ):
        conn.execute(
            text("SELECT * FROM experiments.l2f2_register_execution_artifact(:k, :s, :u, :z)"),
            {"k": "metrics", "s": "a" * 64, "u": "file:///x", "z": 1},
        )


def test_the_registrar_fixes_media_type_and_provenance_itself(service: Any, l2f2: Any) -> None:
    digest = hashlib.sha256(b"synthetic-vcf").hexdigest()
    with service.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT * FROM experiments.l2f2_register_execution_artifact(:k, :s, :u, :z)"),
            {"k": "vcf", "s": digest, "u": "file:///synthetic.vcf", "z": 13},
        )
    with l2f2.engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT media_type, provenance FROM catalog.artifacts WHERE sha256 = :s"),
                {"s": digest},
            )
            .mappings()
            .one()
        )
    assert row["media_type"] == "application/vnd.ga4gh.vcf"
    assert row["provenance"] == "l2f:gatk-vcf"


def test_the_registrar_refuses_a_conflicting_replay(service: Any) -> None:
    digest = hashlib.sha256(b"conflict-probe").hexdigest()
    with service.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT * FROM experiments.l2f2_register_execution_artifact(:k, :s, :u, :z)"),
            {"k": "vcf", "s": digest, "u": "file:///a.vcf", "z": 5},
        )
    with (
        pytest.raises(Exception, match="different metadata"),
        service.connect() as conn,
        conn.begin(),
    ):
        conn.execute(
            text("SELECT * FROM experiments.l2f2_register_execution_artifact(:k, :s, :u, :z)"),
            {"k": "vcf", "s": digest, "u": "file:///moved.vcf", "z": 5},
        )


def test_the_resolve_function_never_exposes_truth(service: Any, l2f2: Any) -> None:
    """Claim + start a job, then confirm the resolved row carries no truth column."""
    plan_hash = l2f2.plan.plan_hash
    with service.connect() as conn, conn.begin():
        claimed = (
            conn.execute(
                text("SELECT job_id FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                {"h": plan_hash, "w": "w-resolve"},
            )
            .mappings()
            .one()
        )
    with service.connect() as conn, conn.begin():
        row = (
            conn.execute(
                text("SELECT * FROM experiments.l2f2_resolve_claimed_execution(:h, :j, :w)"),
                {"h": plan_hash, "j": claimed["job_id"], "w": "w-resolve"},
            )
            .mappings()
            .one()
        )
    columns = set(row)
    assert row["partition"] == "train"
    for forbidden in (
        "truth_vcf_sha256",
        "truth_tbi_sha256",
        "mutations_vcf_sha256",
        "mutations_tbi_sha256",
        "minos_score",
        "evaluation_hash",
    ):
        assert forbidden not in columns
    for required in ("bam_sha256", "reference_sha256", "config_hash", "config_uri"):
        assert required in columns
