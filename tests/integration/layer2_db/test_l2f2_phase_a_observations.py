"""Reading the frozen Phase-A screen back out of the ledgers as ``BaselineObservation``s.

Every distinction the frozen objective depends on is made here, against real ledger rows written
by the production paths: an admitted score is the exact persisted number, a refused admission is
NOT a zero, a candidate's failure and our own infrastructure incident are different outcomes, and
a job that has not been decided yet produces no observation at all rather than a zero-valued one.

No GATK and no MINOS_SUBNET: executions use ``FakeGatkRunner`` through the private test seam and
scores are recorded upstream results. Nothing here touches the real baseline store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from minos_engine.baseline.objective import BaselineObjectiveError, classify_failure_code
from minos_engine.baseline.phase_a_observations import (
    PHASE_A_SCORING_CONTRACT,
    PhaseAObservationError,
    load_phase_a_observations,
)
from minos_engine.storage.l2f2_phase_a_control import (
    expand_l2f2_phase_a_jobs,
    read_l2f2_phase_a_progress,
)
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from tests.integration.layer2_db.l2f2_phase_a_env import close_the_canary, phase_a_store


class _BrokenHarnessRunner:
    """A runner that fails for a reason that is OURS, not the candidate configuration's."""

    gatk_version = "fake-gatk-4.5.0.0"

    def run(self, **_kwargs: Any) -> Any:
        raise RuntimeError("the harness could not dispatch this attempt")


@pytest.fixture
def screen(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    """The frozen canary closed, plus a handful of expanded jobs to decide."""
    with phase_a_store(isolated_pg_base_url, tmp_path) as env:
        close_the_canary(env, minos_score=0.618836338270872)
        expand_l2f2_phase_a_jobs(env.engine, start=1, count=6)
        yield env


def _by_config(snapshot: Any) -> dict[str, Any]:
    return {observation.config_hash: observation for observation in snapshot.observations}


def _canary_observation(env: Any) -> Any:
    snapshot = load_phase_a_observations(env.engine)
    return _by_config(snapshot)[env.authority.canary.config_hash]


def _success_runtime(env: Any, job_key: str) -> int:
    with env.engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT runtime_ms FROM experiments.l2f_execution_results WHERE job_key = :k"),
                {"k": job_key},
            ).scalar_one()
        )


def _failure_runtime(env: Any, job_key: str) -> int:
    with env.engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT runtime_ms FROM experiments.l2f_execution_failures WHERE job_key = :k"
                ),
                {"k": job_key},
            ).scalar_one()
        )


# --------------------------------------------------------------------------- #
# the four decided outcomes
# --------------------------------------------------------------------------- #
def test_an_admitted_evaluation_becomes_the_exact_persisted_score(screen: Any) -> None:
    observation = _canary_observation(screen)

    assert observation.admitted is True
    assert observation.minos_score == 0.618836338270872, "the score is passed through, not derived"
    assert observation.failure_code is None
    assert observation.outcome == "ADMITTED"
    assert observation.dataset_id == screen.authority.canary.dataset_id
    assert observation.chromosome == "chr18"
    assert observation.gatk_runtime_ms == _success_runtime(screen, screen.authority.canary.job_key)


def test_a_refused_admission_is_a_candidate_failure_and_never_a_zero(screen: Any) -> None:
    """The validator refused the score. That is not the same statement as "scored 0"."""
    dispatched = screen.run(worker_id="ci-refused")
    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    screen.evaluate(dispatched, minos_score=0.0, admitted=False, admission_code="NONPOSITIVE_SCORE")

    snapshot = load_phase_a_observations(screen.engine)
    observation = next(o for o in snapshot.observations if o.config_hash != _canary_config(screen))
    assert observation.admitted is False
    assert observation.minos_score is None, "a refused admission must NOT become a score of zero"
    assert observation.failure_code is None, "no bounded code: nothing failed, it was refused"
    assert observation.outcome == "CANDIDATE_FAILURE"
    assert observation.gatk_runtime_ms == _success_runtime(screen, dispatched.job_key)


def _canary_config(env: Any) -> str:
    return str(env.authority.canary.config_hash)


def test_a_gatk_failure_becomes_a_candidate_failure_carrying_its_measured_runtime(
    screen: Any,
) -> None:
    dispatched = screen.run(worker_id="ci-gatk-failed", runner=FakeGatkRunner(exit_code=4))
    assert dispatched is not None and dispatched.status == "FAILED"

    snapshot = load_phase_a_observations(screen.engine)
    observation = next(o for o in snapshot.observations if o.failure_code == "GATK_NONZERO_EXIT")
    assert observation.minos_score is None
    assert observation.admitted is False
    assert observation.outcome == "CANDIDATE_FAILURE"
    # the runtime is the one the runner measured for THIS attempt, not a constant or an average.
    assert observation.gatk_runtime_ms == _failure_runtime(screen, dispatched.job_key)
    assert snapshot.candidate_failure_count == 1
    assert snapshot.infrastructure_incident_count == 0


def test_a_harness_failure_becomes_an_infrastructure_incident(screen: Any) -> None:
    """An ``EXECUTION_ERROR`` is ours. It must not be charged to the candidate."""
    from minos_engine.storage.l2f2_runner import ExecutionRecordedFailureError

    with pytest.raises(ExecutionRecordedFailureError):
        screen.run(worker_id="ci-harness-broke", runner=_BrokenHarnessRunner())

    snapshot = load_phase_a_observations(screen.engine)
    observation = next(o for o in snapshot.observations if o.failure_code == "EXECUTION_ERROR")
    assert observation.outcome == "INFRASTRUCTURE_INCIDENT"
    assert observation.minos_score is None
    assert snapshot.infrastructure_incident_count == 1
    assert snapshot.candidate_failure_count == 0


def test_an_evaluation_failure_carries_the_successful_gatk_runtime(screen: Any) -> None:
    """GATK succeeded and scoring broke: the runtime that belongs to the row is GATK's own."""
    dispatched = screen.run(worker_id="ci-eval-broke")
    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    screen.fail_evaluation(dispatched, failure_code="HAPPY_TIMEOUT")

    snapshot = load_phase_a_observations(screen.engine)
    observation = next(o for o in snapshot.observations if o.failure_code == "HAPPY_TIMEOUT")
    assert observation.outcome == "INFRASTRUCTURE_INCIDENT"
    assert observation.minos_score is None
    assert observation.gatk_runtime_ms == _success_runtime(screen, dispatched.job_key)
    assert classify_failure_code("HAPPY_TIMEOUT") == "INFRASTRUCTURE_INCIDENT"


# --------------------------------------------------------------------------- #
# absence is the representation of "not decided"
# --------------------------------------------------------------------------- #
def test_pending_running_and_unscored_jobs_produce_no_observation_at_all(screen: Any) -> None:
    """None of the three undecided states may enter the objective as a zero."""
    from minos_engine.storage.l2f2_runner import authorize_baseline_runner_connection

    executed = screen.run(worker_id="ci-unscored")  # SUCCEEDED, deliberately not evaluated
    assert executed is not None and executed.status == "SUCCEEDED"

    with screen.service.connect() as conn, conn.begin():  # one job left RUNNING
        authorize_baseline_runner_connection(conn)
        claimed = (
            conn.execute(
                text("SELECT job_id, job_key FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                {"h": screen.plan.plan_hash, "w": "ci-running"},
            )
            .mappings()
            .one()
        )
        conn.execute(
            text("SELECT * FROM experiments.minos_l2f_start_job(:h, :j, :w)"),
            {"h": screen.plan.plan_hash, "j": claimed["job_id"], "w": "ci-running"},
        )

    snapshot = load_phase_a_observations(screen.engine)
    assert len(snapshot.observations) == 1, "only the canary is decided"
    assert snapshot.observations[0].config_hash == _canary_config(screen)
    assert snapshot.execution_result_count == 2, "the unscored execution is still counted"

    progress = read_l2f2_phase_a_progress(screen.engine)
    assert progress.enqueued_count == 7
    assert (progress.pending_count, progress.running_count, progress.succeeded_count) == (4, 1, 2)
    assert progress.decided_observation_count == 1
    assert progress.missing_observation_count == 194
    assert progress.complete is False


# --------------------------------------------------------------------------- #
# states no faithful observation can be derived from
# --------------------------------------------------------------------------- #
def test_an_evaluation_under_another_scoring_contract_is_refused(screen: Any) -> None:
    """Scores from two different contracts are not comparable, so they are never mixed."""
    from minos_engine.evaluation.contracts import build_metrics_artifact_bytes
    from minos_engine.evaluation.evaluator import (
        EvaluationArtifactPublisher,
        build_evaluation_record,
        evaluate_metrics,
        record_evaluation_result,
        register_metrics_artifact,
    )
    from minos_engine.evaluation.scoring_contract import compute_scoring_contract_hash
    from tests.integration.layer2_db.test_l2f2_evaluation_ledger import (
        _authority,
        _inputs_for,
        _oracle_result,
        _scoring_inputs,
    )

    dispatched = screen.run(worker_id="ci-other-contract")
    assert dispatched is not None and dispatched.status == "SUCCEEDED"

    other_commit = "1" * 40
    other = _authority().model_copy(update={"upstream_commit": other_commit})
    assert compute_scoring_contract_hash(other) != PHASE_A_SCORING_CONTRACT
    inputs = _inputs_for(screen, dispatched)
    artifact, admission, _contract = evaluate_metrics(
        inputs=inputs,
        oracle_result=_oracle_result(upstream_commit=other_commit),
        scoring_inputs=_scoring_inputs(),
        authority=other,
    )
    root = screen.tmp_path / "other_contract_artifacts"
    root.mkdir(exist_ok=True)
    root.chmod(0o2750)
    published = EvaluationArtifactPublisher(root).publish(build_metrics_artifact_bytes(artifact))
    artifact_id, _created = register_metrics_artifact(screen.engine, published)
    record_evaluation_result(
        screen.engine,
        build_evaluation_record(
            execution_result_id=screen.execution_result_id(dispatched.job_key),
            inputs=inputs,
            artifact=artifact,
            admission_code=admission,
            authority=other,
            metrics_artifact_id=artifact_id,
            metrics=published,
        ),
    )

    with pytest.raises(PhaseAObservationError, match="not the production contract"):
        load_phase_a_observations(screen.engine)


def test_the_ledger_itself_makes_a_dual_outcome_and_an_unknown_code_unreachable(
    screen: Any,
) -> None:
    """The reader's guards are defence in depth over invariants the database already enforces.

    Both states are refused by the ledger, so a Phase-A screen cannot reach the reader carrying
    them — which is why the reader can treat either as corruption rather than a case to interpret.
    """
    from minos_engine.storage.l2f2_runner import authorize_baseline_runner_connection

    dispatched = screen.run(worker_id="ci-dual")
    assert dispatched is not None and dispatched.status == "SUCCEEDED"

    with (
        pytest.raises(DatabaseError) as dual,
        screen.service.connect() as conn,
        conn.begin(),
    ):
        authorize_baseline_runner_connection(conn)
        conn.execute(
            text("SELECT * FROM experiments.minos_l2f_fail_job(:h, :j, :w, :c, NULL, NULL, 1)"),
            {
                "h": screen.plan.plan_hash,
                "j": screen.job_id(dispatched.job_key),
                "w": "ci-dual",
                "c": "GATK_TIMEOUT",
            },
        )
    # a SUCCEEDED job is not failable at all, so the second outcome never reaches the ledger.
    assert "is not failable by worker" in str(dual.value)

    failed = screen.run(worker_id="ci-unknown-code", runner=FakeGatkRunner(exit_code=6))
    assert failed is not None and failed.status == "FAILED"
    # the reader classifies every persisted code through this function, and it refuses an
    # unbounded one rather than guessing whose fault the failure was.
    with pytest.raises(BaselineObjectiveError, match="unknown bounded failure code"):
        classify_failure_code("NOT_A_BOUNDED_CODE")

    with (
        pytest.raises(DatabaseError) as rejected,
        screen.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                "UPDATE experiments.l2f_execution_failures SET failure_code = 'NOT_A_BOUNDED_CODE'"
            )
        )
    assert "append-only" in str(rejected.value).lower() or "violates" in str(rejected.value)

    # the screen is still readable, and the failure it does carry is a bounded one.
    snapshot = load_phase_a_observations(screen.engine)
    assert {o.failure_code for o in snapshot.observations} == {None, "GATK_NONZERO_EXIT"}
