"""The FROZEN L2-F2 baseline objective — how scored executions are COMPARED.

This module answers a different question from the scoring contract. ``l2f2-minos-scoring-v1``
answers *how one execution is scored*; this answers *how scored executions are aggregated,
compared and promoted*. The two identities are deliberately separate and must never be mixed.

The objective is Option B — lower-tail CVaR, worst-chromosome floor, overall mean, and an
explicit failure penalty:

    J(c) = 0.50 * CVaR_0.25(c) + 0.30 * floor(c) + 0.20 * mean(c) - 1.00 * failure_rate(c)

Three distinctions are load-bearing and are the reason this is not a one-line mean:

* **Aggregation utility is not a score.** A known candidate failure or non-admission contributes
  utility ``0.0`` *to the aggregate only*. It never rewrites the immutable evaluation record and
  never claims the scientific score was zero — the ledger remains the authority on what happened.
* **Failure is not missing.** A known failure is a decided, penalised outcome. A missing
  evaluation is undecided: the candidate is simply not complete and not finally rankable, and may
  participate only through the formally bounded racing rules in :mod:`~minos_engine.baseline.racing`.
* **Candidate failure is not an infrastructure incident.** A configuration that makes GATK fail is
  a property of the candidate. Our evaluation harness failing is a property of *us*, and is
  tracked as phase health rather than charged against the candidate.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from minos_engine.common.errors import MinosEngineError
from minos_engine.experiments.execution_contract import FailureCode as ExecutionFailureCode

__all__ = [
    "CANDIDATE_EXECUTION_FAILURE_CODES",
    "CVAR_ALPHA",
    "CVAR_WEIGHT",
    "FAILURE_PENALTY",
    "FLOOR_WEIGHT",
    "INFRASTRUCTURE_EVALUATION_FAILURE_CODES",
    "INFRASTRUCTURE_EXECUTION_FAILURE_CODES",
    "MEAN_WEIGHT",
    "BaselineObjectiveError",
    "BaselineObservation",
    "CandidateAggregate",
    "aggregate_candidate",
    "classify_failure_code",
    "objective_value",
    "rank_candidates",
    "tie_break_key",
]

#: frozen robustness constants (D3). Changing any of them is a NEW protocol version.
CVAR_ALPHA = 0.25
CVAR_WEIGHT = 0.50
FLOOR_WEIGHT = 0.30
MEAN_WEIGHT = 0.20
FAILURE_PENALTY = 1.00

#: the candidate is responsible: its configuration made GATK fail or produce nothing usable.
CANDIDATE_EXECUTION_FAILURE_CODES: tuple[str, ...] = (
    "GATK_NONZERO_EXIT",
    "GATK_TIMEOUT",
    "GATK_OUTPUT_INVALID",
    "GATK_OUTPUT_MISSING",
)
#: WE are responsible: the harness could not prepare or dispatch the run at all.
INFRASTRUCTURE_EXECUTION_FAILURE_CODES: tuple[str, ...] = (
    "PREPARATION_FAILED",
    "EXECUTION_ERROR",
)
#: every evaluation-side failure is offline evaluation infrastructure, never the candidate's
#: fault: hap.py, truth resolution, artifact publication and persistence are all ours.
INFRASTRUCTURE_EVALUATION_FAILURE_CODES: tuple[str, ...] = (
    "TRUTH_IDENTITY_MISSING",
    "TRUTH_BYTES_MISMATCH",
    "VCF_BYTES_MISMATCH",
    "HAPPY_NONZERO_EXIT",
    "HAPPY_TIMEOUT",
    "HAPPY_OUTPUT_INVALID",
    "SCORER_OUTPUT_INVALID",
    "ARTIFACT_PUBLISH_FAILED",
    "EVALUATION_ERROR",
)

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)

Outcome = Literal["ADMITTED", "CANDIDATE_FAILURE", "INFRASTRUCTURE_INCIDENT"]


class BaselineObjectiveError(MinosEngineError):
    """The observations cannot be aggregated under the frozen protocol."""


def classify_failure_code(code: str) -> Outcome:
    """Split a bounded failure code into candidate responsibility vs phase health.

    The vocabulary is taken from the existing execution and evaluation contracts; nothing is
    invented here, and an unknown code is refused rather than guessed at.
    """
    if code in CANDIDATE_EXECUTION_FAILURE_CODES:
        return "CANDIDATE_FAILURE"
    if code in INFRASTRUCTURE_EXECUTION_FAILURE_CODES:
        return "INFRASTRUCTURE_INCIDENT"
    if code in INFRASTRUCTURE_EVALUATION_FAILURE_CODES:
        return "INFRASTRUCTURE_INCIDENT"
    raise BaselineObjectiveError(f"unknown bounded failure code {code!r}")


class BaselineObservation(BaseModel):
    """ONE decided (candidate, TRAIN member) outcome.

    A missing evaluation is represented by the ABSENCE of an observation, never by an
    observation carrying a zero — that distinction is what keeps "failed" and "not yet run"
    from collapsing into each other.
    """

    model_config = _STRICT

    config_hash: str = Field(min_length=64, max_length=64)
    dataset_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)
    #: the admitted Minos score in [0, 1]; None exactly when this is not an admitted success.
    minos_score: float | None = None
    admitted: bool
    failure_code: str | None = None
    gatk_runtime_ms: int = Field(ge=0)

    @field_validator("minos_score")
    @classmethod
    def _finite_unit_interval(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("minos_score must be finite")
        if not 0.0 <= value <= 1.0:
            raise ValueError("minos_score must lie in [0, 1]")
        return value

    def model_post_init(self, _context: Any) -> None:
        if self.admitted:
            if self.minos_score is None:
                raise ValueError("an admitted observation must carry a minos_score")
            if self.failure_code is not None:
                raise ValueError("an admitted observation cannot carry a failure_code")
        elif self.minos_score is not None:
            raise ValueError(
                "a non-admitted observation must not carry a minos_score; a refused result is "
                "not a low score"
            )

    @property
    def outcome(self) -> Outcome:
        if self.admitted:
            return "ADMITTED"
        if self.failure_code is None:
            # non-admission with no bounded failure code: the validator refused the result.
            return "CANDIDATE_FAILURE"
        return classify_failure_code(self.failure_code)

    @property
    def utility(self) -> float:
        """AGGREGATION utility only — never the scientific score, never written to the ledger."""
        if self.admitted:
            assert self.minos_score is not None  # noqa: S101 - enforced in model_post_init
            return self.minos_score
        return 0.0


class CandidateAggregate(BaseModel):
    """One candidate's aggregate over an EXACT required member set."""

    model_config = _STRICT

    config_hash: str = Field(min_length=64, max_length=64)
    required_count: int = Field(gt=0)
    observed_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    infrastructure_incident_count: int = Field(ge=0)
    cvar: float
    floor: float
    mean: float
    failure_rate: float
    objective: float
    mean_gatk_runtime_ms: float = Field(ge=0.0)

    @property
    def complete(self) -> bool:
        """Only a complete candidate is finally rankable; a partial one may only be raced."""
        return self.observed_count == self.required_count


def _cvar(utilities: Sequence[float], alpha: float = CVAR_ALPHA) -> float:
    """Mean of the ``ceil(alpha * N)`` LOWEST utilities — the lower-tail average."""
    if not utilities:  # pragma: no cover - guarded by the caller
        raise BaselineObjectiveError("cannot compute CVaR over an empty utility vector")
    take = math.ceil(alpha * len(utilities))
    take = max(1, min(take, len(utilities)))
    lowest = sorted(utilities)[:take]
    return sum(lowest) / len(lowest)


def objective_value(*, cvar: float, floor: float, mean: float, failure_rate: float) -> float:
    """The frozen J. Higher is better."""
    return (
        CVAR_WEIGHT * cvar
        + FLOOR_WEIGHT * floor
        + MEAN_WEIGHT * mean
        - FAILURE_PENALTY * failure_rate
    )


def aggregate_candidate(
    *,
    config_hash: str,
    observations: Iterable[BaselineObservation],
    required_members: Sequence[tuple[str, str]],
) -> CandidateAggregate:
    """Aggregate ONE candidate over ``required_members`` as ``(dataset_id, chromosome)`` pairs.

    Observation arrival order cannot change the result: everything is derived from the required
    member set and sorted utilities, never from iteration order.
    """
    if not required_members:
        raise BaselineObjectiveError("the required member set must not be empty")
    required = list(required_members)
    if len({m[0] for m in required}) != len(required):
        raise BaselineObjectiveError("the required member set contains a duplicate dataset_id")
    required_index = dict(required)

    seen: dict[str, BaselineObservation] = {}
    for observation in observations:
        if observation.config_hash != config_hash:
            raise BaselineObjectiveError(
                f"observation for {observation.config_hash} supplied to aggregate {config_hash}"
            )
        if observation.dataset_id not in required_index:
            raise BaselineObjectiveError(
                f"observation for {observation.dataset_id} is not a required member"
            )
        if required_index[observation.dataset_id] != observation.chromosome:
            raise BaselineObjectiveError(
                f"observation for {observation.dataset_id} claims chromosome "
                f"{observation.chromosome!r}, the schedule says "
                f"{required_index[observation.dataset_id]!r}"
            )
        if observation.dataset_id in seen:
            raise BaselineObjectiveError(
                f"duplicate observation for member {observation.dataset_id}"
            )
        seen[observation.dataset_id] = observation

    decided = [seen[d] for d, _c in required if d in seen]
    utilities = [o.utility for o in decided]
    failures = sum(1 for o in decided if o.outcome == "CANDIDATE_FAILURE")
    incidents = sum(1 for o in decided if o.outcome == "INFRASTRUCTURE_INCIDENT")

    if not decided:
        cvar = floor = mean = 0.0
    else:
        cvar = _cvar(utilities)
        mean = sum(utilities) / len(utilities)
        per_chromosome: dict[str, list[float]] = {}
        for observation in decided:
            per_chromosome.setdefault(observation.chromosome, []).append(observation.utility)
        floor = min(sum(v) / len(v) for v in per_chromosome.values())

    # the penalty is charged over the REQUIRED count, so a candidate cannot dilute its failure
    # rate by simply having fewer members evaluated.
    failure_rate = failures / len(required)
    runtimes = [o.gatk_runtime_ms for o in decided]
    return CandidateAggregate(
        config_hash=config_hash,
        required_count=len(required),
        observed_count=len(decided),
        failure_count=failures,
        infrastructure_incident_count=incidents,
        cvar=cvar,
        floor=floor,
        mean=mean,
        failure_rate=failure_rate,
        objective=objective_value(cvar=cvar, floor=floor, mean=mean, failure_rate=failure_rate),
        mean_gatk_runtime_ms=(sum(runtimes) / len(runtimes)) if runtimes else 0.0,
    )


def tie_break_key(aggregate: CandidateAggregate, *, candidate_index: int) -> tuple[Any, ...]:
    """THE total order. Deterministic and total — no unresolved ties are possible.

    1. higher J; 2. lower mean GATK runtime; 3. lower candidate index in the frozen phase
    design; 4. lexicographically smaller config_hash.
    """
    return (
        -aggregate.objective,
        aggregate.mean_gatk_runtime_ms,
        candidate_index,
        aggregate.config_hash,
    )


def rank_candidates(
    aggregates: Iterable[CandidateAggregate], *, candidate_index: dict[str, int]
) -> tuple[CandidateAggregate, ...]:
    """Order candidates best-first under the frozen total order."""
    items = list(aggregates)
    for aggregate in items:
        if aggregate.config_hash not in candidate_index:
            raise BaselineObjectiveError(
                f"candidate {aggregate.config_hash} has no index in the frozen phase design"
            )
    return tuple(
        sorted(
            items, key=lambda a: tie_break_key(a, candidate_index=candidate_index[a.config_hash])
        )
    )


_ = ExecutionFailureCode  # the bounded vocabulary this module classifies, imported for provenance
