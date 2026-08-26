"""CONTROL-PLANE expansion and progress reporting for the frozen L2-F2 Phase-A screen.

Phase A is five chromosome-balanced TRAIN members × the 39 accepted OAT candidates — 195 logical
jobs, frozen. Logical job 0 is the canary, already executed and evaluated; the remaining 194 are
what this module can add, and nothing else.

Three properties are load-bearing.

**The scientific plan is never a caller argument.** Expansion accepts an engine, a ``start`` and a
``count``. The plan, candidate set, member set, config set and every job key are recomputed here
from :func:`build_phase_a_authority`, so an operator can choose *which contiguous slice of the
frozen order* to insert and nothing else — not which candidate, not which member, not a different
plan.

**There is still no enqueue-all.** ``count`` is bounded by ``MAX_ENQUEUE_BATCH`` exactly as the
historical path is, and there is no ``remaining=True`` or ``all=True``. Four explicit calls cover
the 194 remaining jobs, and each one is a deliberate operator act.

**Expansion is a control-plane act, not a scientific one.** It inserts job rows. It does not
re-persist the plan, re-publish a CONFIG payload, create an authority row, run GATK or score
anything — all of which already exist and are immutable.

The canary gate before expansion is deliberately a PIPELINE gate, not a quality gate: it requires
the canary to have reached a durable success and a durable terminal evaluation, and says nothing
whatever about the score it produced. Making expansion conditional on a good score after seeing
one would be exactly the kind of post-hoc protocol change the frozen design exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.l2f_job_enqueue import MAX_ENQUEUE_BATCH

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "CANARY_LOGICAL_INDEX",
    "PHASE_A_LOGICAL_JOB_COUNT",
    "PhaseAExpansionError",
    "PhaseAExpansionResult",
    "PhaseAProgress",
    "expand_l2f2_phase_a_jobs",
    "read_l2f2_phase_a_progress",
]

#: the frozen screen size. Recomputed and cross-checked against committed authority on every call.
PHASE_A_LOGICAL_JOB_COUNT = 195

#: the canary. It is already SUCCEEDED/EVALUATED and is never re-enqueued by this boundary.
CANARY_LOGICAL_INDEX = 0

#: the production scoring contract a terminal canary evaluation must have been recorded under.
_SCORING_CONTRACT = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"

_TERMINAL_JOB_STATES = ("PENDING", "CLAIMED", "RUNNING", "SUCCEEDED", "FAILED")


class PhaseAExpansionError(MinosEngineError):
    """The Phase-A screen may not be expanded from the state the database is in."""


@dataclass(frozen=True)
class PhaseAExpansionResult:
    """What one bounded expansion slice established (or found already present)."""

    start: int
    count: int
    created: int
    existing: int
    jobs_total_after: int


@dataclass(frozen=True)
class PhaseAProgress:
    """A deterministic snapshot of how far the frozen Phase-A screen has actually got."""

    logical_job_count: int
    enqueued_count: int
    pending_count: int
    claimed_count: int
    running_count: int
    succeeded_count: int
    failed_count: int
    execution_result_count: int
    execution_failure_count: int
    evaluation_result_count: int
    evaluation_failure_count: int
    decided_observation_count: int
    candidate_failure_count: int
    infrastructure_incident_count: int
    missing_observation_count: int

    @property
    def complete(self) -> bool:
        """True only when EVERY frozen logical job has one decided observation."""
        return self.decided_observation_count == self.logical_job_count


def _authority() -> Any:
    from minos_engine.baseline.phase_a import build_phase_a_authority

    return build_phase_a_authority()


def _frozen_job_keys() -> list[str]:
    """The 195 frozen job keys in frozen logical order, recomputed from committed authority."""
    from minos_engine.experiments.plan import iter_logical_jobs

    authority = _authority()
    keys = [job.job_key for job in iter_logical_jobs(authority.plan)]
    if len(keys) != PHASE_A_LOGICAL_JOB_COUNT:
        raise PhaseAExpansionError(
            f"the frozen Phase-A plan enumerates {len(keys)} logical jobs, expected "
            f"{PHASE_A_LOGICAL_JOB_COUNT}"
        )
    if keys[CANARY_LOGICAL_INDEX] != authority.canary.job_key:
        raise PhaseAExpansionError("logical job 0 is not the frozen canary")
    if len(set(keys)) != len(keys):
        raise PhaseAExpansionError("the frozen Phase-A plan enumerates a duplicate job key")
    return keys


def _validate_expansion_range(start: int, count: int) -> None:
    """The bounded range contract, checked BEFORE any database access.

    ``start`` begins at 1 because logical job 0 is the completed canary: this boundary cannot
    re-enqueue it even by arithmetic accident.
    """
    for name, value in (("start", start), ("count", count)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise PhaseAExpansionError(f"{name} must be an int, got {type(value).__name__}")
    if start < CANARY_LOGICAL_INDEX + 1:
        raise PhaseAExpansionError(
            f"start must be >= {CANARY_LOGICAL_INDEX + 1}; logical job {CANARY_LOGICAL_INDEX} is "
            "the completed canary and is never re-enqueued"
        )
    if count < 1:
        raise PhaseAExpansionError("count must be >= 1; there is no zero-length expansion")
    if count > MAX_ENQUEUE_BATCH:
        raise PhaseAExpansionError(
            f"count {count} exceeds MAX_ENQUEUE_BATCH {MAX_ENQUEUE_BATCH}; there is deliberately "
            "no enqueue-all API, so the remaining jobs are requested as explicit bounded slices"
        )
    if start + count > PHASE_A_LOGICAL_JOB_COUNT:
        raise PhaseAExpansionError(
            f"slice [{start}, {start + count}) runs past the frozen Phase-A screen of "
            f"{PHASE_A_LOGICAL_JOB_COUNT} logical jobs"
        )


def _verify_existing_jobs(conn: Any, keys: list[str]) -> dict[str, dict[str, Any]]:
    """Prove every Phase-A job in the store is one of the frozen 195, then return them by key.

    The read is SCOPED to the Phase-A plan hash. Another plan legitimately coexists in this store
    once Phase B is persisted, and its jobs are simply not this screen's business; what must never
    be tolerated is a job that CLAIMS the Phase-A plan and does not match a frozen identity, so
    everything inside the scope is verified exactly as strictly as before — unrecognised key,
    wrong member/config binding or duplicate logical identity all still refuse.
    """
    from sqlalchemy import text

    authority = _authority()
    rows = (
        conn.execute(
            text(
                "SELECT j.job_key, j.status, j.claimed_by, p.plan_hash, "
                "       pm.member_index, pc.config_index "
                "  FROM experiments.l2f_experiment_jobs j "
                "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                "  JOIN experiments.l2f_experiment_plan_members pm ON pm.id = j.plan_member_id "
                "  JOIN experiments.l2f_experiment_plan_configs pc ON pc.id = j.plan_config_id "
                " WHERE p.plan_hash = :plan_hash"
            ),
            {"plan_hash": authority.plan_hash},
        )
        .mappings()
        .all()
    )
    index_of = {key: position for position, key in enumerate(keys)}
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["job_key"])
        if str(row["plan_hash"]) != authority.plan_hash:  # pragma: no cover - query-scoped
            raise PhaseAExpansionError(
                f"job {key} belongs to plan {row['plan_hash']}, not the frozen Phase-A plan"
            )
        position = index_of.get(key)
        if position is None:
            raise PhaseAExpansionError(
                f"job {key} is not one of the frozen {PHASE_A_LOGICAL_JOB_COUNT} Phase-A jobs"
            )
        # the frozen logical order is member-major: index = member_index * candidates + config.
        expected_member, expected_config = divmod(position, authority.plan.candidate_count)
        if (
            int(row["member_index"]) != expected_member
            or int(row["config_index"]) != expected_config
        ):
            raise PhaseAExpansionError(
                f"job {key} binds member {row['member_index']}/config {row['config_index']}, but "
                f"its frozen logical index {position} is member {expected_member}/config "
                f"{expected_config}"
            )
        if key in seen:
            raise PhaseAExpansionError(f"job {key} is enqueued more than once")
        if str(row["status"]) not in _TERMINAL_JOB_STATES:
            raise PhaseAExpansionError(f"job {key} has unknown status {row['status']!r}")
        seen[key] = dict(row)
    return seen


def _require_canary_closure(conn: Any) -> None:
    """Require the canary to have closed the PIPELINE — never that it scored well.

    This is a readiness gate over execution and evaluation *reachability*: one durable success,
    one durable terminal evaluation under the production scoring contract, and no failure on
    either side. The score itself is deliberately not consulted; conditioning expansion on the
    canary's number after seeing it would be a protocol change made from a single observation.
    """
    from sqlalchemy import text

    authority = _authority()
    canary = authority.canary.job_key
    row = (
        conn.execute(
            text(
                "SELECT j.id, j.status FROM experiments.l2f_experiment_jobs j "
                "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                " WHERE j.job_key = :k AND p.plan_hash = :plan_hash"
            ),
            {"k": canary, "plan_hash": authority.plan_hash},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise PhaseAExpansionError(
            "the frozen canary is not enqueued; Phase-A preparation has not run"
        )
    if str(row["status"]) != "SUCCEEDED":
        raise PhaseAExpansionError(
            f"the canary is {row['status']}, not SUCCEEDED; the scientific pipeline has not been "
            "proven end to end and the screen must not be expanded"
        )

    counts = (
        conn.execute(
            text(
                "SELECT (SELECT count(*) FROM experiments.l2f_execution_results r "
                "         WHERE r.job_id = :j) AS successes, "
                "       (SELECT count(*) FROM experiments.l2f_execution_failures f "
                "         WHERE f.job_id = :j) AS failures, "
                "       (SELECT count(*) FROM evaluation.l2f_evaluation_results e "
                "         JOIN experiments.l2f_execution_results r2 ON r2.id = e.execution_result_id "
                "        WHERE r2.job_id = :j AND e.scoring_contract_hash = :c) AS evaluations, "
                "       (SELECT count(*) FROM evaluation.l2f_evaluation_failures ef "
                "         JOIN experiments.l2f_execution_results r3 ON r3.id = ef.execution_result_id "
                "        WHERE r3.job_id = :j) AS evaluation_failures"
            ),
            {"j": row["id"], "c": _SCORING_CONTRACT},
        )
        .mappings()
        .one()
    )

    if int(counts["successes"]) != 1 or int(counts["failures"]) != 0:
        raise PhaseAExpansionError(
            f"the canary has {counts['successes']} execution result(s) and "
            f"{counts['failures']} failure(s); exactly one success and no failure is required"
        )
    if int(counts["evaluations"]) != 1 or int(counts["evaluation_failures"]) != 0:
        raise PhaseAExpansionError(
            f"the canary has {counts['evaluations']} terminal evaluation(s) under contract "
            f"{_SCORING_CONTRACT[:12]}… and {counts['evaluation_failures']} evaluation "
            "failure(s); exactly one evaluation and no failure is required"
        )


def expand_l2f2_phase_a_jobs(engine: Engine, *, start: int, count: int) -> PhaseAExpansionResult:
    """Enqueue ONE bounded slice of the frozen Phase-A logical jobs. Idempotent.

    ``start`` and ``count`` are the only choices a caller has, and both are bounded: ``start`` is
    at least 1 (job 0 is the completed canary), ``count`` at most ``MAX_ENQUEUE_BATCH``, and the
    slice must lie inside the frozen 195. Everything scientific is recomputed from committed
    authority.

    A replay inserts nothing and resets nothing: status, ``claimed_by``, ``claimed_at`` and any
    terminal outcome of an existing job are never touched.
    """
    from minos_engine.storage.l2f_job_enqueue import _enqueue_l2f2_phase_a_slice_with_trust

    _validate_expansion_range(start, count)
    keys = _frozen_job_keys()

    with engine.connect() as conn:
        _verify_existing_jobs(conn, keys)
        _require_canary_closure(conn)

    result = _enqueue_l2f2_phase_a_slice_with_trust(engine, start=start, count=count)

    with engine.connect() as conn:
        after = _verify_existing_jobs(conn, keys)
    return PhaseAExpansionResult(
        start=start,
        count=count,
        created=result.created_count,
        existing=result.existing_count,
        jobs_total_after=len(after),
    )


def read_l2f2_phase_a_progress(engine: Engine) -> PhaseAProgress:
    """A deterministic read-only snapshot of the frozen screen's actual state.

    Derived entirely from the immutable ledgers and the committed authority; it enqueues nothing,
    scores nothing and repairs nothing.
    """
    from minos_engine.baseline.phase_a_observations import load_phase_a_observations

    keys = _frozen_job_keys()
    with engine.connect() as conn:
        jobs = _verify_existing_jobs(conn, keys)
    snapshot = load_phase_a_observations(engine)

    statuses = [str(row["status"]) for row in jobs.values()]
    return PhaseAProgress(
        logical_job_count=PHASE_A_LOGICAL_JOB_COUNT,
        enqueued_count=len(jobs),
        pending_count=statuses.count("PENDING"),
        claimed_count=statuses.count("CLAIMED"),
        running_count=statuses.count("RUNNING"),
        succeeded_count=statuses.count("SUCCEEDED"),
        failed_count=statuses.count("FAILED"),
        execution_result_count=snapshot.execution_result_count,
        execution_failure_count=snapshot.execution_failure_count,
        evaluation_result_count=snapshot.evaluation_result_count,
        evaluation_failure_count=snapshot.evaluation_failure_count,
        decided_observation_count=len(snapshot.observations),
        candidate_failure_count=snapshot.candidate_failure_count,
        infrastructure_incident_count=snapshot.infrastructure_incident_count,
        missing_observation_count=PHASE_A_LOGICAL_JOB_COUNT - len(snapshot.observations),
    )
