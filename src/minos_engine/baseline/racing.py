"""FROZEN racing bounds, elimination and seed-controlled promotion.

Racing exists to spend the frozen budget on candidates that can still win. It must never
eliminate a candidate that *could* have survived, so every decision is made on formal bounds over
the eventual complete member vector rather than on a partial average:

* **optimistic** — every unseen member returns utility ``1.0`` and does not fail. This is the
  best the candidate could still become.
* **pessimistic** — every unseen member returns utility ``0.0`` and counts as a failure. This is
  the worst a rival could still become.

A candidate is eliminated only when even its optimistic future is **strictly** worse than the
threshold rival's pessimistic floor. Strictness matters: a candidate that could merely *tie* the
threshold is still alive, because ties are settled by the frozen total order once both are
complete, not by whichever happened to be measured first.

Two further rules keep the race honest. Racing is evaluated only on **complete
chromosome-balanced batches**, so the worst-chromosome floor is never computed on a prefix that
over-represents one chromosome. And the seed is **never** eliminated: the baseline must always be
comparable against the configuration currently in use, even if it is losing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.baseline.objective import (
    BaselineObservation,
    CandidateAggregate,
    aggregate_candidate,
    objective_value,
    tie_break_key,
)
from minos_engine.common.errors import MinosEngineError

__all__ = [
    "PHASE_B_SURVIVOR_COUNT",
    "PHASE_C_ELIMINATION_RANK",
    "PHASE_B_ELIMINATION_RANK",
    "VALIDATION_FINALIST_COUNT",
    "RacingBound",
    "RacingError",
    "eliminate",
    "racing_bounds",
    "select_finalists",
    "select_survivors",
]

#: Phase B promotes ten candidates into Phase C; Phase C promotes four into validation.
PHASE_B_SURVIVOR_COUNT = 10
VALIDATION_FINALIST_COUNT = 4

#: elimination thresholds are the survivor-count-th best pessimistic objective.
PHASE_B_ELIMINATION_RANK = PHASE_B_SURVIVOR_COUNT
PHASE_C_ELIMINATION_RANK = VALIDATION_FINALIST_COUNT

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)

#: the frozen unseen-member assumptions.
_OPTIMISTIC_UTILITY = 1.0
_PESSIMISTIC_UTILITY = 0.0


class RacingError(MinosEngineError):
    """A racing decision cannot be made under the frozen protocol."""


class RacingBound(BaseModel):
    """The optimistic and pessimistic objective bounds for ONE partially observed candidate."""

    model_config = _STRICT

    config_hash: str = Field(min_length=64, max_length=64)
    observed_count: int = Field(ge=0)
    required_count: int = Field(gt=0)
    optimistic: float
    pessimistic: float

    @property
    def complete(self) -> bool:
        return self.observed_count == self.required_count


def _bound(
    *,
    config_hash: str,
    observations: Sequence[BaselineObservation],
    required_members: Sequence[tuple[str, str]],
    unseen_utility: float,
    unseen_fails: bool,
) -> float:
    """Objective over the eventual complete vector, filling unseen members with an assumption."""
    seen = {o.dataset_id: o for o in observations}
    utilities: list[float] = []
    per_chromosome: dict[str, list[float]] = {}
    failures = 0
    for dataset_id, chromosome in required_members:
        observation = seen.get(dataset_id)
        if observation is None:
            utility = unseen_utility
            if unseen_fails:
                failures += 1
        else:
            utility = observation.utility
            if observation.outcome == "CANDIDATE_FAILURE":
                failures += 1
        utilities.append(utility)
        per_chromosome.setdefault(chromosome, []).append(utility)

    take = max(1, min(math.ceil(0.25 * len(utilities)), len(utilities)))
    cvar = sum(sorted(utilities)[:take]) / take
    mean = sum(utilities) / len(utilities)
    floor = min(sum(v) / len(v) for v in per_chromosome.values())
    _ = config_hash
    return objective_value(
        cvar=cvar, floor=floor, mean=mean, failure_rate=failures / len(required_members)
    )


def racing_bounds(
    *,
    config_hash: str,
    observations: Iterable[BaselineObservation],
    required_members: Sequence[tuple[str, str]],
) -> RacingBound:
    """The frozen optimistic/pessimistic bounds over the eventual complete member vector."""
    if not required_members:
        raise RacingError("the required member set must not be empty")
    decided = [o for o in observations if o.config_hash == config_hash]
    required_ids = {d for d, _c in required_members}
    for observation in decided:
        if observation.dataset_id not in required_ids:
            raise RacingError(f"observation for {observation.dataset_id} is not a required member")
    if len({o.dataset_id for o in decided}) != len(decided):
        raise RacingError(f"duplicate observation for candidate {config_hash}")

    return RacingBound(
        config_hash=config_hash,
        observed_count=len(decided),
        required_count=len(required_members),
        optimistic=_bound(
            config_hash=config_hash,
            observations=decided,
            required_members=required_members,
            unseen_utility=_OPTIMISTIC_UTILITY,
            unseen_fails=False,
        ),
        pessimistic=_bound(
            config_hash=config_hash,
            observations=decided,
            required_members=required_members,
            unseen_utility=_PESSIMISTIC_UTILITY,
            unseen_fails=True,
        ),
    )


def eliminate(
    bounds: Iterable[RacingBound], *, seed_config_hash: str, keep: int
) -> tuple[str, ...]:
    """Config hashes that may be eliminated: STRICTLY below the keep-th best pessimistic bound.

    Returns eliminations in deterministic order. The seed is never returned, and a candidate
    whose optimistic bound merely equals the threshold survives.
    """
    items = list(bounds)
    if keep <= 0:
        raise RacingError("keep must be positive")
    if len(items) <= keep:
        return ()
    pessimistic = sorted((b.pessimistic for b in items), reverse=True)
    threshold = pessimistic[keep - 1]
    return tuple(
        sorted(
            b.config_hash
            for b in items
            if b.config_hash != seed_config_hash and b.optimistic < threshold
        )
    )


def _promote(
    *,
    aggregates: Mapping[str, CandidateAggregate],
    candidate_index: Mapping[str, int],
    seed_config_hash: str,
    count: int,
) -> tuple[str, ...]:
    """Seed-controlled promotion: the natural top ``count``, or the top ``count - 1`` plus seed.

    The result is always exactly ``count`` candidates and always contains the seed, so every
    later phase can still measure the winner against the configuration in use today.
    """
    complete = [a for a in aggregates.values() if a.complete]
    if seed_config_hash not in aggregates:
        raise RacingError("the seed must be present in every promotion decision")
    if len(complete) < count:
        raise RacingError(
            f"only {len(complete)} complete candidates; {count} promotions are required"
        )
    ordered = sorted(
        complete, key=lambda a: tie_break_key(a, candidate_index=candidate_index[a.config_hash])
    )
    natural = [a.config_hash for a in ordered[:count]]
    if seed_config_hash in natural:
        return tuple(natural)
    kept = [a.config_hash for a in ordered if a.config_hash != seed_config_hash][: count - 1]
    return (*kept, seed_config_hash)


def select_survivors(
    *,
    aggregates: Mapping[str, CandidateAggregate],
    candidate_index: Mapping[str, int],
    seed_config_hash: str,
    count: int = PHASE_B_SURVIVOR_COUNT,
) -> tuple[str, ...]:
    """Phase-B → Phase-C promotion: exactly ten, always including the seed."""
    return _promote(
        aggregates=aggregates,
        candidate_index=candidate_index,
        seed_config_hash=seed_config_hash,
        count=count,
    )


def select_finalists(
    *,
    aggregates: Mapping[str, CandidateAggregate],
    candidate_index: Mapping[str, int],
    seed_config_hash: str,
    count: int = VALIDATION_FINALIST_COUNT,
) -> tuple[str, ...]:
    """Phase-C → validation promotion: exactly four, always including the seed.

    These four config hashes are frozen BEFORE any validation truth is made available.
    """
    return _promote(
        aggregates=aggregates,
        candidate_index=candidate_index,
        seed_config_hash=seed_config_hash,
        count=count,
    )


_ = aggregate_candidate  # re-exported by callers that build aggregates before promoting
