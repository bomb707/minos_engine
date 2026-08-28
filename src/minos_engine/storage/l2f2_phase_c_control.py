"""CONTROL-PLANE materialization, racing, progress and finalist selection for Phase C.

Phase C confirms ten promoted configurations across the whole 50-member TRAIN partition. 500 is a
CEILING, not a quota — that is the entire point of racing here. Ten balanced batches, and after
each complete one the frozen rule eliminates whoever can no longer reach the top four. A campaign
that spends fewer than 500 pairs has not failed; it has succeeded at the thing racing is for.

Three boundaries are load-bearing, and they are the same ones Phase B established.

**Racing is evaluated only on a COMPLETE balanced batch** — every candidate still alive entering
batch N must hold a decided observation for each of that batch's five chromosomes. A partial batch
would let job completion order decide who survives.

**Elimination is recomputed from the ledger, never supplied.** Asking for batch N+1's jobs cannot
carry a survivor list; the survivors are re-derived from the immutable observations every time.

**An infrastructure incident stops everything.** Those are our failures, and racing over them would
let a defect of ours eliminate a candidate.

One thing is Phase C's own: the frozen tie-break's candidate index is each candidate's ORIGINAL
Phase-B design position, not its 0..9 position here. See
:func:`~minos_engine.baseline.design.phase_c_inherited_candidate_index`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.l2f_job_enqueue import MAX_ENQUEUE_BATCH

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from minos_engine.baseline.phase_c import PhaseCAuthority

__all__ = [
    "PhaseCExpansionError",
    "PhaseCExpansionResult",
    "PhaseCProgress",
    "PhaseCRacingDecision",
    "eligible_phase_c_batch_jobs",
    "expand_l2f2_phase_c_batch",
    "race_l2f2_phase_c_batch",
    "read_l2f2_phase_c_progress",
    "select_l2f2_validation_finalists",
]


class PhaseCExpansionError(MinosEngineError):
    """The Phase-C confirmation may not be advanced from the state the database is in."""


@dataclass(frozen=True)
class PhaseCExpansionResult:
    """What one bounded Phase-C materialization slice established (or found already present)."""

    batch_index: int
    start: int
    count: int
    created: int
    existing: int
    eligible_total: int
    jobs_total_after: int
    surviving_candidate_count: int


@dataclass(frozen=True)
class PhaseCRacingDecision:
    """The frozen racing outcome after ONE complete balanced batch."""

    batch_index: int
    eliminated_config_hashes: tuple[str, ...]
    surviving_config_hashes: tuple[str, ...]
    seed_config_hash: str
    keep: int

    @property
    def survivor_count(self) -> int:
        return len(self.surviving_config_hashes)


@dataclass(frozen=True)
class PhaseCProgress:
    """A deterministic, plan-scoped, read-only snapshot of the Phase-C confirmation."""

    logical_job_budget: int
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
    completed_batch_count: int
    alive_candidate_count: int
    eliminated_candidate_count: int
    complete_candidate_count: int
    decided_by_candidate: dict[str, int]
    complete: bool


def _authority(engine: Engine) -> PhaseCAuthority:
    from minos_engine.baseline.phase_c import build_l2f2_phase_c_authority

    return build_l2f2_phase_c_authority(engine)


def _observations_by_config(engine: Engine, authority: PhaseCAuthority) -> dict[str, list[Any]]:
    from minos_engine.baseline.phase_c_observations import load_phase_c_observations

    snapshot = load_phase_c_observations(engine, authority=authority)
    if snapshot.infrastructure_incident_count:
        raise PhaseCExpansionError(
            f"Phase C holds {snapshot.infrastructure_incident_count} infrastructure incident(s); "
            "advancing over our own failures could eliminate a candidate for something we broke"
        )
    by_config: dict[str, list[Any]] = {h: [] for h in authority.ordered_config_hashes}
    for observation in snapshot.observations:
        if observation.config_hash in by_config:
            by_config[observation.config_hash].append(observation)
    return by_config


def eligible_phase_c_batch_jobs(
    authority: PhaseCAuthority, *, batch_index: int, survivors: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    """The internally derived job-key sequence ONE batch may materialize, in frozen order."""
    from minos_engine.baseline.phase_c import PHASE_C_BATCH_COUNT
    from minos_engine.experiments.plan import iter_logical_jobs

    if batch_index not in range(PHASE_C_BATCH_COUNT):
        raise PhaseCExpansionError(
            f"batch_index {batch_index} outside 0..{PHASE_C_BATCH_COUNT - 1}"
        )
    members = set(authority.batch_members(batch_index))
    allowed = set(authority.ordered_config_hashes) if survivors is None else set(survivors)
    order = {h: i for i, h in enumerate(authority.ordered_config_hashes)}
    jobs = [
        job
        for job in iter_logical_jobs(authority.plan)
        if job.member_index in members and job.config_hash in allowed
    ]
    jobs.sort(key=lambda j: (order[j.config_hash], j.member_index))
    return tuple(job.job_key for job in jobs)


def _persisted_jobs(conn: Any, authority: PhaseCAuthority) -> dict[str, dict[str, Any]]:
    """Every persisted Phase-C job, verified against the frozen logical identities. Plan-scoped."""
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
            raise PhaseCExpansionError(f"job {key} is not one of the frozen Phase-C logical jobs")
        if key in seen:
            raise PhaseCExpansionError(f"job {key} is enqueued more than once")
        if (
            str(row["config_hash"]) != job.config_hash
            or int(row["member_index"]) != job.member_index
        ):
            raise PhaseCExpansionError(
                f"job {key} binds member {row['member_index']}/config {row['config_hash']}, which "
                "is not its frozen identity"
            )
        seen[key] = dict(row)
    return seen


def _batch_is_complete(
    authority: PhaseCAuthority,
    by_config: dict[str, list[Any]],
    *,
    batch_index: int,
    alive: tuple[str, ...],
) -> bool:
    """Every candidate ALIVE entering this batch holds a decided observation for all five."""
    from minos_engine.baseline.phase_c import PHASE_C_BATCH_SIZE

    pairs = set(authority.required_pairs(batch_index + 1)) - set(
        authority.required_pairs(batch_index) if batch_index else ()
    )
    for config_hash in alive:
        decided = {(o.dataset_id, o.chromosome) for o in by_config.get(config_hash, [])} & pairs
        if len(decided) != PHASE_C_BATCH_SIZE:
            return False
    return True


def race_l2f2_phase_c_batch(
    engine: Engine, *, batch_index: int, authority: PhaseCAuthority | None = None
) -> PhaseCRacingDecision:
    """The frozen racing decision after ONE complete balanced batch.

    Bounds are taken over the FULL fifty-member requirement — optimistic fills unseen members with
    utility 1, pessimistic with a failure — so a candidate is eliminated only when even its best
    possible remainder could not reach the fourth-best pessimistic bound. Ties survive and the seed
    is never eliminated.

    The decision is computed from the ledger AS IT STANDS, not from the ledger as it stood when
    batch ``batch_index`` closed. During a campaign those are the same thing, because the next
    batch is materialized before any of it is decided. Afterwards they are not: replaying batch 0
    against a finished confirmation sees all fifty members and may eliminate candidates the live
    campaign carried further. That is the frozen rule behaving correctly — more observation
    narrows the bounds and can never widen them, so elimination is monotone and no candidate is
    ever resurrected — but it does mean the per-batch elimination list is a decision, not a
    historical record. What actually happened to the queue is durable in the queue.
    """
    from minos_engine.baseline.phase_c import PHASE_C_BATCH_COUNT
    from minos_engine.baseline.racing import (
        PHASE_C_ELIMINATION_RANK,
        eliminate,
        racing_bounds,
    )

    resolved = authority or _authority(engine)
    if batch_index not in range(PHASE_C_BATCH_COUNT):
        raise PhaseCExpansionError(
            f"batch_index {batch_index} outside 0..{PHASE_C_BATCH_COUNT - 1}"
        )
    by_config = _observations_by_config(engine, resolved)
    alive = _survivors_through(engine, resolved, through_batch=batch_index - 1, by_config=by_config)
    if not _batch_is_complete(resolved, by_config, batch_index=batch_index, alive=alive):
        raise PhaseCExpansionError(
            f"batch {batch_index} is not complete: a candidate still alive entering it lacks a "
            "decided observation on all five of its chromosomes. Racing on a partial batch would "
            "let job completion order decide which candidates survive"
        )

    required = resolved.required_pairs()
    bounds = [
        racing_bounds(config_hash=h, observations=by_config[h], required_members=required)
        for h in alive
    ]
    eliminated = eliminate(
        bounds, seed_config_hash=resolved.seed_config_hash, keep=PHASE_C_ELIMINATION_RANK
    )
    removed = set(eliminated)
    surviving = tuple(h for h in alive if h not in removed)
    return PhaseCRacingDecision(
        batch_index=batch_index,
        eliminated_config_hashes=tuple(eliminated),
        surviving_config_hashes=surviving,
        seed_config_hash=resolved.seed_config_hash,
        keep=PHASE_C_ELIMINATION_RANK,
    )


def _survivors_through(
    engine: Engine,
    authority: PhaseCAuthority,
    *,
    through_batch: int,
    by_config: dict[str, list[Any]] | None = None,
) -> tuple[str, ...]:
    """Who is still alive after racing every complete batch up to and including ``through_batch``.

    Recomputed from the immutable ledger on every call. There is no stored survivor list, so a
    caller cannot hand one in and an eliminated candidate cannot be resurrected by asking twice.
    """
    alive = authority.ordered_config_hashes
    if through_batch < 0:
        return alive
    observations = (
        by_config if by_config is not None else _observations_by_config(engine, authority)
    )
    for index in range(through_batch + 1):
        if not _batch_is_complete(authority, observations, batch_index=index, alive=alive):
            raise PhaseCExpansionError(
                f"batch {index} is not complete; the survivor set through batch {through_batch} "
                "cannot be derived"
            )
        decision = race_l2f2_phase_c_batch(engine, batch_index=index, authority=authority)
        alive = decision.surviving_config_hashes
    return alive


def _validate_range(start: int, count: int, *, eligible: int) -> None:
    for name, value in (("start", start), ("count", count)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise PhaseCExpansionError(f"{name} must be an int, got {type(value).__name__}")
    if start < 0:
        raise PhaseCExpansionError("start must be >= 0")
    if count < 1:
        raise PhaseCExpansionError("count must be >= 1; there is no zero-length materialization")
    if count > MAX_ENQUEUE_BATCH:
        raise PhaseCExpansionError(
            f"count {count} exceeds MAX_ENQUEUE_BATCH {MAX_ENQUEUE_BATCH}; there is deliberately "
            "no enqueue-all API, so a batch is materialized in explicit bounded slices"
        )
    if start + count > eligible:
        raise PhaseCExpansionError(
            f"slice [{start}, {start + count}) runs past this batch's {eligible} eligible jobs"
        )


def expand_l2f2_phase_c_batch(
    engine: Engine, *, batch_index: int, start: int, count: int
) -> PhaseCExpansionResult:
    """Materialize ONE bounded slice of a Phase-C batch. Idempotent.

    ``batch_index``, ``start`` and ``count`` are the only caller choices, and none is scientific:
    which candidates a batch may contain is derived here — every promoted candidate for batch 0,
    and thereafter whoever the frozen racing rule still permits, recomputed from the immutable
    ledger on every call.
    """
    authority = _authority(engine)
    survivors = None
    surviving_count = len(authority.ordered_config_hashes)
    if batch_index != 0:
        survivors = _survivors_through(engine, authority, through_batch=batch_index - 1)
        surviving_count = len(survivors)
        if not survivors:  # pragma: no cover - the seed is never eliminated
            raise PhaseCExpansionError("no candidate survives; there is nothing to materialize")
    eligible = eligible_phase_c_batch_jobs(authority, batch_index=batch_index, survivors=survivors)
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
    return PhaseCExpansionResult(
        batch_index=batch_index,
        start=start,
        count=count,
        created=created,
        existing=len(wanted) - len(missing),
        eligible_total=len(eligible),
        jobs_total_after=len(after),
        surviving_candidate_count=surviving_count,
    )


def _materialize(engine: Engine, authority: PhaseCAuthority, wanted: list[str]) -> int:
    """Insert exactly the named frozen jobs through the bounded enqueue seam."""
    from minos_engine.experiments.plan import iter_logical_jobs
    from minos_engine.storage.l2f_job_enqueue import _enqueue_l2f2_phase_c_slice_with_trust

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
            result = _enqueue_l2f2_phase_c_slice_with_trust(engine, start=offset, count=chunk)
            created += result.created_count
            offset += chunk
            remaining -= chunk
    return created


def read_l2f2_phase_c_progress(engine: Engine) -> PhaseCProgress:
    """A deterministic, plan-scoped, read-only snapshot of the Phase-C confirmation's state."""
    from minos_engine.baseline.phase_c import (
        PHASE_C_BATCH_COUNT,
        PHASE_C_LOGICAL_JOB_BUDGET,
        PHASE_C_MEMBER_COUNT,
    )
    from minos_engine.baseline.phase_c_observations import load_phase_c_observations
    from minos_engine.baseline.racing import VALIDATION_FINALIST_COUNT

    authority = _authority(engine)
    with engine.connect() as conn:
        jobs = _persisted_jobs(conn, authority)
    snapshot = load_phase_c_observations(engine, authority=authority)

    statuses = [str(row["status"]) for row in jobs.values()]
    by_config: dict[str, list[Any]] = {h: [] for h in authority.ordered_config_hashes}
    for observation in snapshot.observations:
        if observation.config_hash in by_config:
            by_config[observation.config_hash].append(observation)
    decided_by_candidate = {h: len(obs) for h, obs in by_config.items()}

    completed_batches = 0
    alive: tuple[str, ...] = authority.ordered_config_hashes
    eliminated = 0
    if not snapshot.infrastructure_incident_count:
        for index in range(PHASE_C_BATCH_COUNT):
            if not _batch_is_complete(authority, by_config, batch_index=index, alive=alive):
                break
            completed_batches += 1
            decision = race_l2f2_phase_c_batch(engine, batch_index=index, authority=authority)
            eliminated += len(decision.eliminated_config_hashes)
            alive = decision.surviving_config_hashes

    complete_candidates = sum(
        1 for h in alive if decided_by_candidate.get(h, 0) == PHASE_C_MEMBER_COUNT
    )
    complete = (
        completed_batches == PHASE_C_BATCH_COUNT
        and complete_candidates == len(alive)
        and complete_candidates >= VALIDATION_FINALIST_COUNT
        and decided_by_candidate.get(authority.seed_config_hash, 0) == PHASE_C_MEMBER_COUNT
        and not snapshot.infrastructure_incident_count
    )
    return PhaseCProgress(
        logical_job_budget=PHASE_C_LOGICAL_JOB_BUDGET,
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
        completed_batch_count=completed_batches,
        alive_candidate_count=len(alive),
        eliminated_candidate_count=eliminated,
        complete_candidate_count=complete_candidates,
        decided_by_candidate=decided_by_candidate,
        complete=complete,
    )


def select_l2f2_validation_finalists(engine: Engine) -> tuple[str, ...]:
    """Phase-C → validation promotion: exactly four configurations, always including the seed.

    Only complete, non-eliminated candidates are ranked. An eliminated candidate legitimately
    stopped where racing stopped it and is never resurrected or completed on paper.

    The tie-break's candidate index is each candidate's ORIGINAL Phase-B design position, never its
    0..9 position in this promotion — see
    :func:`~minos_engine.baseline.design.phase_c_inherited_candidate_index`.
    """
    from minos_engine.baseline.objective import aggregate_candidate
    from minos_engine.baseline.phase_c import PHASE_C_BATCH_COUNT, PHASE_C_MEMBER_COUNT
    from minos_engine.baseline.racing import VALIDATION_FINALIST_COUNT, select_finalists

    authority = _authority(engine)
    progress = read_l2f2_phase_c_progress(engine)
    if progress.infrastructure_incident_count:
        raise PhaseCExpansionError(
            "Phase C holds an infrastructure incident; finalists are never frozen over one"
        )
    if progress.completed_batch_count != PHASE_C_BATCH_COUNT:
        raise PhaseCExpansionError(
            f"only {progress.completed_batch_count} of {PHASE_C_BATCH_COUNT} balanced batches are "
            "complete; the TRAIN ranking is not final"
        )
    by_config = _observations_by_config(engine, authority)
    alive = _survivors_through(
        engine, authority, through_batch=PHASE_C_BATCH_COUNT - 1, by_config=by_config
    )
    required = list(authority.required_pairs())

    aggregates = {}
    for config_hash in alive:
        aggregate = aggregate_candidate(
            config_hash=config_hash,
            observations=by_config[config_hash],
            required_members=required,
        )
        if not aggregate.complete:
            raise PhaseCExpansionError(
                f"surviving candidate {config_hash} has {aggregate.observed_count} of "
                f"{PHASE_C_MEMBER_COUNT} required observations; promotion never ranks a candidate "
                "whose TRAIN confirmation is unfinished"
            )
        aggregates[config_hash] = aggregate
    if len(aggregates) < VALIDATION_FINALIST_COUNT:
        raise PhaseCExpansionError(
            f"only {len(aggregates)} complete candidate(s); {VALIDATION_FINALIST_COUNT} finalists "
            "are required"
        )
    return select_finalists(
        aggregates=aggregates,
        candidate_index=authority.inherited_candidate_index,
        seed_config_hash=authority.seed_config_hash,
    )
