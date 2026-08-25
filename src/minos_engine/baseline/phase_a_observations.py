"""Read the frozen Phase-A screen out of the immutable ledgers as BaselineObservations.

This is the join between what actually happened — GATK executions, MINOS_SUBNET evaluations,
bounded failures — and the frozen objective's input type. It reads the database and nothing else:
no CSV, no operator-supplied score, no caller-supplied dataset/config mapping, and no defaults.
The 5 members, 39 candidates and 195 logical identities are recomputed from
:func:`build_phase_a_authority`.

The mapping is deliberately narrow, because each case means something different to the objective:

* **not yet decided** (PENDING / CLAIMED / RUNNING) — NO observation at all. A missing result is
  represented by absence, never by a zero, which is what keeps "failed" and "not yet run" from
  collapsing into each other;
* **success + evaluated + admitted** — the exact persisted upstream ``minos_score``;
* **success + evaluated + not admitted** — ``admitted=False``, ``minos_score=None`` and no failure
  code. The validator refused the result; that is a candidate failure by the existing
  ``BaselineObservation`` semantics, and it is emphatically not a score of zero;
* **GATK failed** — the exact bounded execution failure code, with the elapsed attempt runtime
  the runner measured;
* **success + evaluation failed** — the exact bounded evaluation failure code, with the runtime
  of the GATK execution that did succeed.

Whether a failure is the candidate's fault or ours is decided by the existing
:func:`classify_failure_code`; this module does not re-classify anything.

Nothing here is best-effort. A state that cannot exist under the ledger's own invariants — a
SUCCEEDED job with no result, a job carrying both outcomes, an evaluation under a different
scoring contract, a config or dataset that disagrees with the frozen plan — is refused, because
aggregating over a corrupt screen would silently produce a scientifically meaningless number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.baseline.objective import BaselineObservation, classify_failure_code
from minos_engine.common.errors import MinosEngineError

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "PHASE_A_SCORING_CONTRACT",
    "PhaseAObservationError",
    "PhaseAObservationSnapshot",
    "load_phase_a_observations",
]

#: the production scoring contract every Phase-A evaluation must have been recorded under.
PHASE_A_SCORING_CONTRACT = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"

#: job states that carry no decided outcome yet. Their absence IS the information.
_UNDECIDED = ("PENDING", "CLAIMED", "RUNNING")


class PhaseAObservationError(MinosEngineError):
    """The Phase-A ledgers are in a state no faithful observation can be derived from."""


@dataclass(frozen=True)
class PhaseAObservationSnapshot:
    """Every decided Phase-A outcome, plus the ledger counts they were derived from."""

    observations: tuple[BaselineObservation, ...]
    execution_result_count: int
    execution_failure_count: int
    evaluation_result_count: int
    evaluation_failure_count: int

    @property
    def candidate_failure_count(self) -> int:
        return sum(1 for o in self.observations if o.outcome == "CANDIDATE_FAILURE")

    @property
    def infrastructure_incident_count(self) -> int:
        return sum(1 for o in self.observations if o.outcome == "INFRASTRUCTURE_INCIDENT")

    @property
    def admitted_count(self) -> int:
        return sum(1 for o in self.observations if o.outcome == "ADMITTED")


def _fail(message: str) -> None:
    raise PhaseAObservationError(message)


def load_phase_a_observations(engine: Engine) -> PhaseAObservationSnapshot:
    """Derive every DECIDED Phase-A observation from the immutable ledgers."""
    from sqlalchemy import text

    from minos_engine.baseline.phase_a import build_phase_a_authority
    from minos_engine.experiments.plan import iter_logical_jobs

    authority = build_phase_a_authority()
    plan = authority.plan
    frozen = {job.job_key: job for job in iter_logical_jobs(plan)}
    member_chromosome = {m.dataset_id: None for m in plan.members}

    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT j.id AS job_id, j.job_key, j.status, p.plan_hash, "
                    "       dr.dataset_id, dr.chromosome, pc.config_hash, pc.config_index, "
                    "       pm.member_index, "
                    "       r.id AS execution_result_id, r.runtime_ms AS success_runtime_ms, "
                    "       f.failure_code AS execution_failure_code, "
                    "       f.runtime_ms AS failure_runtime_ms, "
                    "       e.id AS evaluation_id, e.minos_score, e.admitted, "
                    "       e.admission_code, e.scoring_contract_hash, "
                    "       ef.failure_code AS evaluation_failure_code "
                    "  FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                    "  JOIN experiments.l2f_experiment_plan_members pm ON pm.id = j.plan_member_id "
                    "  JOIN experiments.l2f_experiment_plan_configs pc ON pc.id = j.plan_config_id "
                    "  JOIN catalog.dataset_registry dr ON dr.id = pm.dataset_registry_id "
                    "  LEFT JOIN experiments.l2f_execution_results r ON r.job_id = j.id "
                    "  LEFT JOIN experiments.l2f_execution_failures f ON f.job_id = j.id "
                    "  LEFT JOIN evaluation.l2f_evaluation_results e "
                    "         ON e.execution_result_id = r.id "
                    "  LEFT JOIN evaluation.l2f_evaluation_failures ef "
                    "         ON ef.execution_result_id = r.id "
                    " ORDER BY pm.member_index, pc.config_index"
                )
            )
            .mappings()
            .all()
        )
        counts = (
            conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM experiments.l2f_execution_results) AS successes, "
                    "       (SELECT count(*) FROM experiments.l2f_execution_failures) AS failures, "
                    "       (SELECT count(*) FROM evaluation.l2f_evaluation_results) AS evaluations, "
                    "       (SELECT count(*) FROM evaluation.l2f_evaluation_failures) AS eval_failures"
                )
            )
            .mappings()
            .one()
        )

    observations: list[BaselineObservation] = []
    seen_keys: set[str] = set()
    seen_evaluations: set[str] = set()

    for row in rows:
        key = str(row["job_key"])
        if str(row["plan_hash"]) != plan.plan_hash:
            _fail(f"job {key} belongs to a plan other than the frozen Phase-A plan")
        job = frozen.get(key)
        if job is None:
            _fail(f"job {key} is not one of the frozen Phase-A logical jobs")
        assert job is not None  # noqa: S101 - narrowed by _fail
        if key in seen_keys:
            _fail(f"job {key} appears more than once in the ledger join")
        seen_keys.add(key)

        # the frozen identity must still be the persisted one.
        if str(row["config_hash"]) != job.config_hash:
            _fail(f"job {key} binds config {row['config_hash']}, frozen is {job.config_hash}")
        if str(row["dataset_id"]) != job.dataset_id:
            _fail(f"job {key} binds dataset {row['dataset_id']}, frozen is {job.dataset_id}")
        if int(row["member_index"]) != job.member_index:
            _fail(f"job {key} binds member {row['member_index']}, frozen is {job.member_index}")
        if str(row["dataset_id"]) not in member_chromosome:
            _fail(f"job {key} names a dataset outside the frozen Phase-A member set")

        status = str(row["status"])
        success_id = row["execution_result_id"]
        execution_failure = row["execution_failure_code"]
        evaluation_id = row["evaluation_id"]
        evaluation_failure = row["evaluation_failure_code"]

        if success_id is not None and execution_failure is not None:
            _fail(f"job {key} carries BOTH an execution success and an execution failure")
        if evaluation_id is not None and evaluation_failure is not None:
            _fail(f"job {key} carries BOTH an evaluation success and an evaluation failure")
        if (evaluation_id is not None or evaluation_failure is not None) and success_id is None:
            _fail(f"job {key} has an evaluation outcome without a successful execution")

        if status in _UNDECIDED:
            if success_id is not None or execution_failure is not None:
                _fail(f"job {key} is {status} yet already carries a terminal execution outcome")
            continue  # NOT an observation: absence is how "not yet decided" is represented.

        if status == "SUCCEEDED":
            if success_id is None:
                _fail(f"job {key} is SUCCEEDED with no execution result")
            observation = _observation_for_success(row, job)
            if observation is None:
                continue  # executed but not yet evaluated: still undecided.
            evaluation_key = str(evaluation_id) if evaluation_id is not None else None
            if evaluation_key is not None:
                if evaluation_key in seen_evaluations:
                    _fail(f"evaluation {evaluation_key} is bound to more than one job")
                seen_evaluations.add(evaluation_key)
            observations.append(observation)
            continue

        if status == "FAILED":
            if execution_failure is None:
                _fail(f"job {key} is FAILED with no execution failure row")
            runtime = row["failure_runtime_ms"]
            if runtime is None:
                _fail(
                    f"job {key} failed without a recorded elapsed runtime; the frozen objective "
                    "uses mean GATK runtime as a tie-break and will not accept an invented one"
                )
            code = str(execution_failure)
            classify_failure_code(code)  # refuses an unknown bounded code
            observations.append(
                BaselineObservation(
                    config_hash=job.config_hash,
                    dataset_id=job.dataset_id,
                    chromosome=str(row["chromosome"]),
                    minos_score=None,
                    admitted=False,
                    failure_code=code,
                    gatk_runtime_ms=int(runtime),
                )
            )
            continue

        _fail(f"job {key} has unknown status {status!r}")

    return PhaseAObservationSnapshot(
        observations=tuple(observations),
        execution_result_count=int(counts["successes"]),
        execution_failure_count=int(counts["failures"]),
        evaluation_result_count=int(counts["evaluations"]),
        evaluation_failure_count=int(counts["eval_failures"]),
    )


def _observation_for_success(row: Any, job: Any) -> BaselineObservation | None:
    """Map ONE successful execution to its observation, or None if it is not yet evaluated."""
    key = str(row["job_key"])
    runtime = row["success_runtime_ms"]
    if runtime is None:
        _fail(f"job {key} succeeded without a recorded runtime")
    chromosome = str(row["chromosome"])

    evaluation_failure = row["evaluation_failure_code"]
    if evaluation_failure is not None:
        code = str(evaluation_failure)
        classify_failure_code(code)
        return BaselineObservation(
            config_hash=job.config_hash,
            dataset_id=job.dataset_id,
            chromosome=chromosome,
            minos_score=None,
            admitted=False,
            failure_code=code,
            # GATK succeeded; the measurement that belongs here is its own runtime.
            gatk_runtime_ms=int(runtime),
        )

    if row["evaluation_id"] is None:
        return None  # executed, not yet scored — still undecided.

    if str(row["scoring_contract_hash"]) != PHASE_A_SCORING_CONTRACT:
        _fail(
            f"job {key} was evaluated under scoring contract {row['scoring_contract_hash']}, not "
            f"the production contract {PHASE_A_SCORING_CONTRACT}"
        )

    admitted = bool(row["admitted"])
    if not admitted:
        # the validator refused the score. That is a candidate failure with NO bounded code — and
        # deliberately not a score of zero.
        return BaselineObservation(
            config_hash=job.config_hash,
            dataset_id=job.dataset_id,
            chromosome=chromosome,
            minos_score=None,
            admitted=False,
            failure_code=None,
            gatk_runtime_ms=int(runtime),
        )

    score = row["minos_score"]
    if score is None:
        _fail(f"job {key} is ADMITTED with no persisted minos_score")
    return BaselineObservation(
        config_hash=job.config_hash,
        dataset_id=job.dataset_id,
        chromosome=chromosome,
        minos_score=float(score),
        admitted=True,
        failure_code=None,
        gatk_runtime_ms=int(runtime),
    )
