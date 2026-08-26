"""CONTROL-PLANE materialization, progress, racing and promotion for the derived Phase-B screen.

Phase B is 48 configurations × 10 TRAIN members — but 480 is a BUDGET CEILING, not a quota. The
frozen racing rule exists precisely so the second batch is spent only on candidates that could
still win, and a candidate eliminated after batch 0 correctly ends its life at 5 of 10
observations rather than being counted as missing work.

Three boundaries are load-bearing here.

**Racing is evaluated only on a COMPLETE balanced batch.** Not after a chromosome, not after some
number of jobs, not after a convenient subset. Every candidate must have been observed on all five
of batch 0's chromosomes before anything is eliminated, because the objective's floor term is a
per-chromosome minimum and a partial batch would let the order jobs finished in decide who
survives.

**Elimination is recomputed from the ledger, never supplied.** A caller asking for batch-1 jobs
cannot hand in a survivor list; the source re-derives batch-0 bounds and re-runs the frozen
`eliminate` every time, so the queue can only ever contain candidates the immutable data still
permits.

**An infrastructure incident stops everything.** Those are our failures. Racing over a screen that
contains one would let a defect of ours eliminate a candidate, which is exactly the mistake the
first Phase-A campaign made at a smaller scale.

Materializing a job is not authorizing it. Executing one additionally requires the store to be at
``0016`` and a prepared ``PHASE_B`` execution authority for this exact plan
(:mod:`~minos_engine.storage.l2f2_phase_b_prepare`) — an administrative act this module never
performs as a side effect of filling a queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.l2f_job_enqueue import MAX_ENQUEUE_BATCH

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from minos_engine.baseline.phase_b import PhaseBAuthority

__all__ = [
    "PhaseBExpansionError",
    "PhaseBExpansionResult",
    "PhaseBProgress",
    "PhaseBRacingDecision",
    "eligible_batch_jobs",
    "expand_l2f2_phase_b_batch",
    "read_l2f2_phase_b_progress",
    "select_l2f2_phase_c_candidates",
    "race_l2f2_phase_b_batch0",
]


class PhaseBExpansionError(MinosEngineError):
    """The Phase-B screen may not be advanced from the state the database is in."""


@dataclass(frozen=True)
class PhaseBExpansionResult:
    """What one bounded Phase-B materialization slice established (or found already present)."""

    batch_index: int
    start: int
    count: int
    created: int
    existing: int
    eligible_total: int
    jobs_total_after: int


@dataclass(frozen=True)
class PhaseBRacingDecision:
    """The frozen batch-0 racing outcome: who may consume batch 1, and who may not."""

    eliminated_config_hashes: tuple[str, ...]
    surviving_config_hashes: tuple[str, ...]
    seed_config_hash: str
    keep: int

    @property
    def survivor_count(self) -> int:
        return len(self.surviving_config_hashes)


@dataclass(frozen=True)
class PhaseBProgress:
    """A deterministic, plan-scoped snapshot of how far Phase B has actually got."""

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
    batch0_decided_count: int
    batch0_complete: bool
    batch1_eligible_candidate_count: int
    batch1_enqueued_count: int
    batch1_decided_count: int
    complete_candidate_count: int
    complete: bool


def _authority(engine: Engine) -> PhaseBAuthority:
    from minos_engine.baseline.phase_b import build_l2f2_phase_b_authority

    return build_l2f2_phase_b_authority(engine)


def eligible_batch_jobs(
    authority: PhaseBAuthority, *, batch_index: int, survivors: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    """The internally derived job-key sequence ONE batch may materialize, in frozen order.

    Batch 0 is every candidate × members 0..4. Batch 1 is the NON-ELIMINATED candidates ×
    members 5..9 — candidate order follows the frozen design, never the order survivors happened
    to be computed in.
    """
    from minos_engine.baseline.phase_b import PHASE_B_BATCH_COUNT
    from minos_engine.experiments.plan import iter_logical_jobs

    if batch_index not in range(PHASE_B_BATCH_COUNT):
        raise PhaseBExpansionError(
            f"batch_index {batch_index} outside 0..{PHASE_B_BATCH_COUNT - 1}"
        )
    members = set(authority.batch_members(batch_index))
    allowed = set(authority.design.ordered_config_hashes) if survivors is None else set(survivors)
    ordered_configs = {h: i for i, h in enumerate(authority.design.ordered_config_hashes)}
    jobs = [
        job
        for job in iter_logical_jobs(authority.plan)
        if job.member_index in members and job.config_hash in allowed
    ]
    jobs.sort(key=lambda j: (ordered_configs[j.config_hash], j.member_index))
    return tuple(job.job_key for job in jobs)


def _persisted_jobs(conn: Any, authority: PhaseBAuthority) -> dict[str, dict[str, Any]]:
    """Every persisted Phase-B job, verified against the frozen logical identities. Plan-scoped."""
    from sqlalchemy import text

    from minos_engine.experiments.plan import iter_logical_jobs

    frozen = {job.job_key: job for job in iter_logical_jobs(authority.plan)}
    rows = (
        conn.execute(
            text(
                "SELECT j.job_key, j.status, j.claimed_by, pm.member_index, pc.config_index, "
                "       pc.config_hash "
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
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["job_key"])
        job = frozen.get(key)
        if job is None:
            raise PhaseBExpansionError(f"job {key} is not one of the frozen Phase-B logical jobs")
        if key in seen:
            raise PhaseBExpansionError(f"job {key} is enqueued more than once")
        if (
            str(row["config_hash"]) != job.config_hash
            or int(row["member_index"]) != job.member_index
        ):
            raise PhaseBExpansionError(
                f"job {key} binds member {row['member_index']}/config {row['config_hash']}, which "
                "is not its frozen identity"
            )
        seen[key] = dict(row)
    return seen


def _validate_range(start: int, count: int, *, eligible: int) -> None:
    for name, value in (("start", start), ("count", count)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise PhaseBExpansionError(f"{name} must be an int, got {type(value).__name__}")
    if start < 0:
        raise PhaseBExpansionError("start must be >= 0")
    if count < 1:
        raise PhaseBExpansionError("count must be >= 1; there is no zero-length materialization")
    if count > MAX_ENQUEUE_BATCH:
        raise PhaseBExpansionError(
            f"count {count} exceeds MAX_ENQUEUE_BATCH {MAX_ENQUEUE_BATCH}; there is deliberately "
            "no enqueue-all API, so a batch is materialized in explicit bounded slices"
        )
    if start + count > eligible:
        raise PhaseBExpansionError(
            f"slice [{start}, {start + count}) runs past this batch's {eligible} eligible jobs"
        )


def expand_l2f2_phase_b_batch(
    engine: Engine, *, batch_index: int, start: int, count: int
) -> PhaseBExpansionResult:
    """Materialize ONE bounded slice of a Phase-B batch. Idempotent.

    ``batch_index``, ``start`` and ``count`` are the only caller choices, and none of them is
    scientific: which candidates a batch may contain is derived here — every candidate for batch 0,
    and for batch 1 the survivors the frozen racing rule permits, recomputed from the immutable
    batch-0 ledger on every call.
    """

    authority = _authority(engine)
    survivors = None
    if batch_index != 0:
        decision = race_l2f2_phase_b_batch0(engine, authority=authority)
        survivors = decision.surviving_config_hashes
    eligible = eligible_batch_jobs(authority, batch_index=batch_index, survivors=survivors)
    _validate_range(start, count, eligible=len(eligible))

    with engine.connect() as conn:
        before = _persisted_jobs(conn, authority)
    wanted = eligible[start : start + count]
    missing = [key for key in wanted if key not in before]

    created = 0
    if missing:
        created = _materialize(engine, authority, missing)
    with engine.connect() as conn:
        after = _persisted_jobs(conn, authority)
    return PhaseBExpansionResult(
        batch_index=batch_index,
        start=start,
        count=count,
        created=created,
        existing=len(wanted) - len(missing),
        eligible_total=len(eligible),
        jobs_total_after=len(after),
    )


def _materialize(engine: Engine, authority: PhaseBAuthority, wanted: list[str]) -> int:
    """Insert exactly the named frozen jobs through the bounded enqueue seam.

    The seam takes contiguous slices of the plan's own logical order, so a batch's jobs — which are
    contiguous per candidate but not globally — are inserted as the minimal set of runs covering
    exactly the wanted keys. No key outside ``wanted`` is ever created.
    """
    from minos_engine.experiments.plan import iter_logical_jobs
    from minos_engine.storage.l2f_job_enqueue import _enqueue_l2f2_phase_b_slice_with_trust

    index_of = {job.job_key: i for i, job in enumerate(iter_logical_jobs(authority.plan))}
    indices = sorted(index_of[key] for key in wanted)
    runs: list[tuple[int, int]] = []
    for index in indices:
        if runs and index == runs[-1][0] + runs[-1][1]:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((index, 1))

    created = 0
    for run_start, run_length in runs:
        remaining = run_length
        offset = run_start
        while remaining:
            chunk = min(remaining, MAX_ENQUEUE_BATCH)
            result = _enqueue_l2f2_phase_b_slice_with_trust(engine, start=offset, count=chunk)
            created += result.created_count
            offset += chunk
            remaining -= chunk
    return created


def race_l2f2_phase_b_batch0(
    engine: Engine, *, authority: PhaseBAuthority | None = None
) -> PhaseBRacingDecision:
    """The frozen racing decision, computed ONLY on a complete batch 0.

    Bounds are taken over the FULL ten-member requirement — optimistic fills unseen members with
    utility 1, pessimistic with a failure — so a candidate is eliminated only when even its best
    possible remaining five could not reach the tenth-best pessimistic bound. Ties survive and the
    seed is never eliminated.
    """
    from minos_engine.baseline.phase_b import PHASE_B_BATCH_SIZE, PHASE_B_CANDIDATE_COUNT
    from minos_engine.baseline.phase_b_observations import load_phase_b_observations
    from minos_engine.baseline.racing import PHASE_B_SURVIVOR_COUNT, eliminate, racing_bounds

    resolved = authority or _authority(engine)
    snapshot = load_phase_b_observations(engine, authority=resolved)
    if snapshot.infrastructure_incident_count:
        raise PhaseBExpansionError(
            f"Phase B holds {snapshot.infrastructure_incident_count} infrastructure incident(s); "
            "racing over our own failures could eliminate a candidate for something we broke"
        )

    batch0 = set(resolved.required_pairs(1))
    by_config: dict[str, list[Any]] = {h: [] for h in resolved.design.ordered_config_hashes}
    for observation in snapshot.observations:
        pair = (observation.dataset_id, observation.chromosome)
        if pair in batch0 and observation.config_hash in by_config:
            by_config[observation.config_hash].append(observation)

    incomplete = [h for h, obs in by_config.items() if len(obs) != PHASE_B_BATCH_SIZE]
    if incomplete:
        raise PhaseBExpansionError(
            f"batch 0 is not complete: {len(incomplete)} of {PHASE_B_CANDIDATE_COUNT} candidates "
            "lack a decided observation on all five batch-0 chromosomes. Racing on a partial "
            "batch would let job completion order decide which candidates survive"
        )

    required = resolved.required_pairs()
    bounds = [
        racing_bounds(config_hash=h, observations=obs, required_members=required)
        for h, obs in by_config.items()
    ]
    eliminated = eliminate(
        bounds, seed_config_hash=resolved.seed_config_hash, keep=PHASE_B_SURVIVOR_COUNT
    )
    removed = set(eliminated)
    surviving = tuple(h for h in resolved.design.ordered_config_hashes if h not in removed)
    return PhaseBRacingDecision(
        eliminated_config_hashes=tuple(eliminated),
        surviving_config_hashes=surviving,
        seed_config_hash=resolved.seed_config_hash,
        keep=PHASE_B_SURVIVOR_COUNT,
    )


def read_l2f2_phase_b_progress(engine: Engine) -> PhaseBProgress:
    """A deterministic, plan-scoped, read-only snapshot of the Phase-B screen's actual state."""
    from minos_engine.baseline.phase_b import (
        PHASE_B_BATCH_SIZE,
        PHASE_B_LOGICAL_JOB_COUNT,
    )
    from minos_engine.baseline.phase_b_observations import load_phase_b_observations

    authority = _authority(engine)
    with engine.connect() as conn:
        jobs = _persisted_jobs(conn, authority)
    snapshot = load_phase_b_observations(engine, authority=authority)

    statuses = [str(row["status"]) for row in jobs.values()]
    batch0 = set(authority.required_pairs(1))
    batch1_keys = set(eligible_batch_jobs(authority, batch_index=1))
    decided_pairs: dict[str, set[tuple[str, str]]] = {
        h: set() for h in authority.design.ordered_config_hashes
    }
    for observation in snapshot.observations:
        if observation.config_hash in decided_pairs:
            decided_pairs[observation.config_hash].add(
                (observation.dataset_id, observation.chromosome)
            )
    batch0_decided = sum(len(pairs & batch0) for pairs in decided_pairs.values())
    batch0_complete = all(
        len(pairs & batch0) == PHASE_B_BATCH_SIZE for pairs in decided_pairs.values()
    )
    required = set(authority.required_pairs())
    complete_candidates = sum(1 for pairs in decided_pairs.values() if pairs >= required)

    eligible_batch1 = 0
    if batch0_complete and not snapshot.infrastructure_incident_count:
        eligible_batch1 = race_l2f2_phase_b_batch0(engine, authority=authority).survivor_count

    return PhaseBProgress(
        logical_job_count=PHASE_B_LOGICAL_JOB_COUNT,
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
        batch0_decided_count=batch0_decided,
        batch0_complete=batch0_complete,
        batch1_eligible_candidate_count=eligible_batch1,
        batch1_enqueued_count=sum(1 for key in jobs if key in batch1_keys),
        batch1_decided_count=sum(len(pairs - batch0) for pairs in decided_pairs.values()),
        complete_candidate_count=complete_candidates,
        complete=batch0_complete
        and eligible_batch1 > 0
        and complete_candidates == eligible_batch1
        and not snapshot.infrastructure_incident_count,
    )


def select_l2f2_phase_c_candidates(engine: Engine) -> tuple[str, ...]:
    """Phase-B → Phase-C promotion: exactly ten configurations, always including the seed.

    Only NON-ELIMINATED candidates with a complete ten-member aggregate are ranked. An eliminated
    candidate legitimately stops at five observations and is not resurrected, and its unseen second
    batch is never fabricated to make an aggregate look complete.
    """
    from minos_engine.baseline.objective import aggregate_candidate
    from minos_engine.baseline.phase_b_observations import load_phase_b_observations
    from minos_engine.baseline.racing import select_survivors

    authority = _authority(engine)
    decision = race_l2f2_phase_b_batch0(engine, authority=authority)
    snapshot = load_phase_b_observations(engine, authority=authority)
    required = list(authority.required_pairs())

    by_config: dict[str, list[Any]] = {h: [] for h in decision.surviving_config_hashes}
    for observation in snapshot.observations:
        if observation.config_hash in by_config:
            by_config[observation.config_hash].append(observation)

    aggregates = {}
    for config_hash, observations in by_config.items():
        aggregate = aggregate_candidate(
            config_hash=config_hash, observations=observations, required_members=required
        )
        if not aggregate.complete:
            raise PhaseBExpansionError(
                f"surviving candidate {config_hash} has {aggregate.observed_count} of "
                f"{aggregate.required_count} required observations; promotion never ranks a "
                "candidate whose second batch is unfinished"
            )
        aggregates[config_hash] = aggregate

    return select_survivors(
        aggregates=aggregates,
        candidate_index=authority.design.candidate_index,
        seed_config_hash=authority.seed_config_hash,
    )
