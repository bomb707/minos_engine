"""THE L2-F2-F validation control plane. Forty evaluations, no racing, one ranking at the end.

Phase C's control plane exists to decide things during a campaign: which candidates a batch may
contain, who survives it, when the next batch may be materialized. This one deliberately decides
almost nothing, because validation has almost nothing left to decide — the four configurations were
frozen before any validation byte existed, and each of them receives all ten VALIDATION members.

What is here:

* :func:`eligible_l2f2_validation_jobs` — the forty frozen logical jobs, in deterministic order.
  It takes no survivor set and no batch index, because there are neither;
* :func:`read_l2f2_validation_progress` — a read-only, plan-scoped snapshot;
* :func:`rank_l2f2_validation_finalists` — the final ranking, which refuses to run until all forty
  observations are decided, no infrastructure incident exists, and every finalist is complete on
  every member.

What is deliberately NOT here, and cannot be added by configuration:

* **no racing.** There is no ``race_*`` function, no bound, no elimination rank and no import of
  :mod:`minos_engine.baseline.racing`'s elimination machinery. A finalist that scores badly on its
  first three members still receives its other seven. That is the whole point of a confirmation:
  the finalists were chosen by TRAIN evidence, and validation reports how they do, it does not
  re-run the search;
* **no re-selection.** Nothing here returns a finalist set. The four are an input, verified from
  the frozen artifact, and the ranking's output is an ORDER over those four, never a membership
  decision. A validation result cannot change who was validated.

The ranking uses the committed robust objective — the same ``aggregate_candidate`` and the same
``tie_break_key`` that ranked TRAIN — because a confirmation measured with a different instrument
confirms nothing. No post-hoc metric is invented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.baseline.phase_d import (
    PHASE_D_LOGICAL_JOB_BUDGET,
    PhaseDAuthority,
    PhaseDError,
    ValidationPair,
)
from minos_engine.common.errors import MinosEngineError

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from minos_engine.baseline.objective import BaselineObservation

__all__ = [
    "ValidationControlError",
    "ValidationProgress",
    "ValidationRanking",
    "ValidationRankingEntry",
    "eligible_l2f2_validation_jobs",
    "rank_l2f2_validation_finalists",
    "rank_validation_observations",
    "read_l2f2_validation_progress",
]


class ValidationControlError(MinosEngineError):
    """The validation confirmation cannot be advanced or ranked as the frozen protocol requires."""


@dataclass(frozen=True, slots=True)
class ValidationProgress:
    """A deterministic, plan-scoped, read-only snapshot. No decision is taken to produce it."""

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
    decided_by_finalist: dict[str, int]
    complete_finalist_count: int
    complete: bool


@dataclass(frozen=True, slots=True)
class ValidationRankingEntry:
    """One finalist's complete validation evidence. Identity, aggregate, and position."""

    rank: int
    config_hash: str
    inherited_candidate_index: int
    observed_count: int
    candidate_failure_count: int
    cvar: float
    floor: float
    mean: float
    failure_rate: float
    objective: float
    mean_gatk_runtime_ms: float


@dataclass(frozen=True, slots=True)
class ValidationRanking:
    """The ORDER over the four frozen finalists. Never a membership decision."""

    plan_hash: str
    finalist_freeze_sha256: str
    ordered_config_hashes: tuple[str, ...]
    entries: tuple[ValidationRankingEntry, ...]
    seed_config_hash: str
    observation_count: int

    @property
    def leader(self) -> str:
        """The finalist the validation evidence ranks first. Not a promotion — a measurement."""
        return self.entries[0].config_hash


def eligible_l2f2_validation_jobs(authority: PhaseDAuthority) -> tuple[ValidationPair, ...]:
    """The forty frozen logical jobs, in deterministic order.

    There is no ``survivors`` argument and no ``batch_index``: every finalist receives every
    member, and that is fixed by the plan rather than discovered while running it.
    """
    pairs = authority.pairs()
    if len(pairs) != PHASE_D_LOGICAL_JOB_BUDGET:  # pragma: no cover - authority verifies this
        raise ValidationControlError(
            f"the validation authority yields {len(pairs)} logical jobs, the frozen protocol "
            f"fixes {PHASE_D_LOGICAL_JOB_BUDGET}"
        )
    return pairs


def _observations_by_finalist(
    authority: PhaseDAuthority, observations: list[BaselineObservation] | tuple[Any, ...]
) -> dict[str, list[Any]]:
    by_config: dict[str, list[Any]] = {h: [] for h in authority.ordered_config_hashes}
    for observation in observations:
        if observation.config_hash in by_config:
            by_config[observation.config_hash].append(observation)
    return by_config


def read_l2f2_validation_progress(
    engine: Engine, *, authority: PhaseDAuthority, plan: Any
) -> ValidationProgress:
    """A read-only snapshot of the validation confirmation, scoped to its own persisted plan.

    ``plan`` is the persisted validation :class:`ExperimentPlan`; it lives in the SEPARATE
    validation store, never in the closed TRAIN baseline. The observations are read through the
    same shared plan-scoped reader every other phase uses, so a validation observation is derived
    from the immutable ledgers by exactly the rules a TRAIN one is.
    """
    from sqlalchemy import text

    from minos_engine.baseline.plan_observations import load_plan_observations

    snapshot = load_plan_observations(engine, plan=plan, label="validation")
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT j.status FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                    " WHERE p.plan_hash = :plan_hash"
                ),
                {"plan_hash": authority.plan_hash},
            )
            .scalars()
            .all()
        )
    statuses = [str(s) for s in rows]

    by_config = _observations_by_finalist(authority, snapshot.observations)
    decided_by_finalist = {h: len(obs) for h, obs in by_config.items()}
    complete = sum(1 for n in decided_by_finalist.values() if n == authority.member_count)

    # "not admitted" is TWO different things, and conflating them would charge our own failures to
    # a finalist. The split is made once, by ``BaselineObservation.outcome`` /
    # ``classify_failure_code``, and surfaced by the snapshot; this module only reads the verdict.
    candidate_failures = snapshot.candidate_failure_count
    incidents = snapshot.infrastructure_incident_count

    return ValidationProgress(
        logical_job_budget=PHASE_D_LOGICAL_JOB_BUDGET,
        enqueued_count=len(statuses),
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
        candidate_failure_count=candidate_failures,
        infrastructure_incident_count=incidents,
        decided_by_finalist=decided_by_finalist,
        complete_finalist_count=complete,
        complete=(
            len(snapshot.observations) == PHASE_D_LOGICAL_JOB_BUDGET
            and complete == authority.candidate_count
            # ANY infrastructure incident withholds completion, whichever side produced it: an
            # execution-side PREPARATION_FAILED is as much our failure as a scoring one.
            and not incidents
            and not snapshot.evaluation_failure_count
        ),
    )


def rank_validation_observations(
    authority: PhaseDAuthority,
    observations: list[BaselineObservation] | tuple[Any, ...],
    *,
    infrastructure_incident_count: int = 0,
) -> ValidationRanking:
    """Rank the four frozen finalists over their COMPLETE validation evidence.

    Refuses unless the confirmation is whole. That is not pedantry: a ranking computed over
    thirty-seven observations would silently favour whichever finalists happened to finish, and it
    would look exactly like a ranking computed over forty.

    Takes observations directly rather than an engine so the rule can be exercised without a
    database — and so there is no code path in which "rank" also means "read whatever is there".
    """
    from minos_engine.baseline.objective import aggregate_candidate, tie_break_key

    if infrastructure_incident_count:
        raise ValidationControlError(
            f"validation holds {infrastructure_incident_count} infrastructure incident(s); "
            "ranking finalists over our own failures would attribute them to the candidates"
        )
    if len(observations) != PHASE_D_LOGICAL_JOB_BUDGET:
        raise ValidationControlError(
            f"validation ranking requires all {PHASE_D_LOGICAL_JOB_BUDGET} observations, "
            f"found {len(observations)}"
        )

    by_config = _observations_by_finalist(authority, observations)
    required = authority.required_pairs()
    for config_hash, observed in by_config.items():
        if len(observed) != authority.member_count:
            raise ValidationControlError(
                f"finalist {config_hash} has {len(observed)} of {authority.member_count} "
                "validation observations; every finalist receives every member"
            )
        seen = {(o.dataset_id, o.chromosome) for o in observed}
        if seen != set(required):
            raise ValidationControlError(
                f"finalist {config_hash} was not evaluated on exactly the ten frozen VALIDATION "
                "members"
            )

    aggregates = {
        config_hash: aggregate_candidate(
            config_hash=config_hash, observations=observed, required_members=required
        )
        for config_hash, observed in by_config.items()
    }
    ordered = sorted(
        authority.ordered_config_hashes,
        key=lambda h: tie_break_key(
            aggregates[h], candidate_index=authority.inherited_candidate_index[h]
        ),
    )
    entries = tuple(
        ValidationRankingEntry(
            rank=position,
            config_hash=config_hash,
            inherited_candidate_index=authority.inherited_candidate_index[config_hash],
            observed_count=aggregates[config_hash].observed_count,
            candidate_failure_count=aggregates[config_hash].failure_count,
            cvar=aggregates[config_hash].cvar,
            floor=aggregates[config_hash].floor,
            mean=aggregates[config_hash].mean,
            failure_rate=aggregates[config_hash].failure_rate,
            objective=aggregates[config_hash].objective,
            mean_gatk_runtime_ms=aggregates[config_hash].mean_gatk_runtime_ms,
        )
        for position, config_hash in enumerate(ordered, start=1)
    )
    if {e.config_hash for e in entries} != set(authority.ordered_config_hashes):
        raise ValidationControlError(  # pragma: no cover - sorted over the same set
            "the validation ranking does not cover exactly the frozen finalists"
        )
    return ValidationRanking(
        plan_hash=authority.plan_hash,
        finalist_freeze_sha256=authority.finalist_freeze_sha256,
        ordered_config_hashes=authority.ordered_config_hashes,
        entries=entries,
        seed_config_hash=authority.seed_config_hash,
        observation_count=len(observations),
    )


def rank_l2f2_validation_finalists(
    engine: Engine, *, authority: PhaseDAuthority, plan: Any
) -> ValidationRanking:
    """Read the complete validation evidence and rank the four finalists.

    A thin database entry over :func:`rank_validation_observations`; every rule lives there. This
    function does NOT promote anything — L2-F2-F's closure audits the complete forty-observation
    evidence first, and promotion is a later, separately authorized decision.
    """
    from minos_engine.baseline.plan_observations import load_plan_observations

    snapshot = load_plan_observations(engine, plan=plan, label="validation")
    return rank_validation_observations(
        authority,
        snapshot.observations,
        # the authoritative count, covering execution-side AND evaluation-side incidents. An
        # evaluation failure becomes a decided observation carrying its bounded code, so one
        # counter sees both; ``evaluation_failure_count`` alone saw only half of them.
        infrastructure_incident_count=snapshot.infrastructure_incident_count,
    )


def _reject_racing(*_args: Any, **_kwargs: Any) -> None:
    """There is no racing in validation. Present as a named refusal, not as an absence.

    A reader looking for the elimination step should find this rather than nothing at all, and a
    caller reaching for one gets a typed error naming the reason instead of an ``AttributeError``.
    """
    raise PhaseDError(
        "L2-F2-F does not race. Every frozen finalist receives every VALIDATION member, so there "
        "is no elimination step, no optimistic/pessimistic bound and no threshold rank here. A "
        "finalist that scores badly is still evaluated on all ten members — that is what a "
        "confirmation is."
    )
