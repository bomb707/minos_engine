"""Rebuild the forty Phase-D observations from the ledger, and close the campaign on them.

Everything scientific here is delegated. The objective is
:func:`~minos_engine.baseline.objective.aggregate_candidate`, the total order is
:func:`~minos_engine.baseline.objective.rank_candidates`, the outcome vocabulary is
:func:`~minos_engine.baseline.objective.classify_failure_code`, and the final rule is
:func:`~minos_engine.baseline.phase_d_selection.select_phase_d_baseline_from_ranked_hashes`.
This module contributes no mathematics of its own — it decides only which rows are ADMISSIBLE,
and refuses whenever that question has more than one answer.

What it refuses, and why each refusal exists
--------------------------------------------
* fewer or more than the exact frozen 4x10 — a partial matrix is not finally rankable, and an
  extra row means something entered aggregation that the freeze never authorised;
* a second terminal evaluation of the same execution under the FROZEN contract — ambiguity about
  which outcome is real must never be resolved by "latest" or "highest";
* any evaluation under a DIFFERENT scoring contract — those rows are ignored entirely rather than
  averaged in, because they were produced under different semantics;
* any INFRASTRUCTURE_INCIDENT — that is OUR defect, and ranking finalists over it would charge a
  candidate for a machine's failure;
* more than one execution environment — four candidates compared across different environments
  are not comparable at all.

A missing evaluation is the ABSENCE of an observation, never a zero. Utility zero is an
aggregate-level reading of a decided failure, and it is produced by ``BaselineObservation.utility``
alone — never written here, and never written to the ledger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.baseline.objective import (
    BaselineObservation,
    CandidateAggregate,
    aggregate_candidate,
    classify_failure_code,
    rank_candidates,
)
from minos_engine.baseline.phase_d import PhaseDAuthority
from minos_engine.baseline.phase_d_selection import (
    INHERITED_CANDIDATE_INDEX,
    SEED_CONFIG_HASH,
    compute_selection_interpretation_hash,
    select_phase_d_baseline_from_ranked_hashes,
    selection_interpretation_content,
)
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "ACCEPTED_EXECUTION_ENVIRONMENT_HASH",
    "ACCEPTED_MINOS_SUBNET_COMMIT",
    "ACCEPTED_SCORING_CONTRACT_HASH",
    "PHASE_D_CLOSURE_DOMAIN",
    "PHASE_D_CLOSURE_SCHEMA",
    "CandidateClosure",
    "PhaseDClosure",
    "PhaseDClosureError",
    "ClosureObservation",
    "build_phase_d_closure",
    "compute_phase_d_closure_hash",
    "derive_phase_d_observations",
]

PHASE_D_CLOSURE_SCHEMA: Final = "l2f2-phase-d-validation-closure-v1"
PHASE_D_CLOSURE_DOMAIN: Final = "minos:l2f2-phase-d-validation-closure:v1\n"

ACCEPTED_SCORING_CONTRACT_HASH: Final = (
    "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
)
ACCEPTED_MINOS_SUBNET_COMMIT: Final = "649bb92c6abccebde58a736a2b2af7fd77a701c1"
ACCEPTED_EXECUTION_ENVIRONMENT_HASH: Final = (
    "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
)

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class PhaseDClosureError(MinosEngineError):
    """The Phase-D matrix cannot be closed as presented."""


class ClosureObservation(BaseModel):
    """One frozen pair's terminal identity, bound into the closure hash.

    The aggregate numbers alone would not stop one evaluation being swapped for another, so the
    closure commits to the row identities that produced them.
    """

    model_config = _STRICT

    member_index: int = Field(ge=0)
    config_index: int = Field(ge=0)
    config_hash: str = Field(min_length=64, max_length=64)
    dataset_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)
    job_key: str = Field(min_length=64, max_length=64)
    execution_result_id: str | None = None
    execution_result_hash: str | None = None
    execution_failure_id: str | None = None
    gatk_runtime_ms: int = Field(ge=0)
    evaluation_id: str | None = None
    evaluation_hash: str | None = None
    evaluation_failure_id: str | None = None
    scoring_contract_hash: str | None = None
    admitted: bool
    minos_score: float | None = None
    failure_code: str | None = None


class CandidateClosure(BaseModel):
    """One finalist's complete aggregate and its rank under the frozen total order."""

    model_config = _STRICT

    config_hash: str = Field(min_length=64, max_length=64)
    inherited_candidate_index: int = Field(ge=0)
    observed_count: int = Field(ge=0)
    candidate_failure_count: int = Field(ge=0)
    infrastructure_incident_count: int = Field(ge=0)
    cvar: float
    floor: float
    mean: float
    failure_rate: float
    objective: float
    mean_gatk_runtime_ms: float = Field(ge=0.0)
    rank: int = Field(ge=0)


class PhaseDClosure(BaseModel):
    """THE canonical Phase-D validation closure."""

    model_config = _STRICT

    schema_version: str = PHASE_D_CLOSURE_SCHEMA
    baseline_protocol_hash: str = Field(min_length=64, max_length=64)
    selection_interpretation_hash: str = Field(min_length=64, max_length=64)
    selection_interpretation_status: str = Field(min_length=1)
    phase_d_plan_hash: str = Field(min_length=64, max_length=64)
    finalist_freeze_sha256: str = Field(min_length=64, max_length=64)
    phase_c_closure_sha256: str = Field(min_length=64, max_length=64)
    execution_environment_hash: str = Field(min_length=64, max_length=64)
    scoring_contract_hash: str = Field(min_length=64, max_length=64)
    minos_subnet_sha: str = Field(min_length=40, max_length=40)
    candidate_count: int
    member_count: int
    observation_count: int
    candidates: tuple[CandidateClosure, ...]
    observations: tuple[ClosureObservation, ...]
    ordered_ranking: tuple[str, ...]
    selected_config_hash: str = Field(min_length=64, max_length=64)
    seed_config_hash: str = Field(min_length=64, max_length=64)
    seed_rank: int = Field(ge=0)

    def content(self) -> dict[str, Any]:
        """Exactly what ``closure_hash`` covers.

        Scientific inputs, aggregates, order and outcome. No timestamp, path, hostname, database
        name or operator identity: the same evidence closed on another machine must hash the same.
        """
        return {
            "baseline_protocol_hash": self.baseline_protocol_hash,
            "candidate_count": self.candidate_count,
            "candidates": [
                {
                    "config_hash": c.config_hash,
                    "cvar": c.cvar,
                    "failure_rate": c.failure_rate,
                    "floor": c.floor,
                    "candidate_failure_count": c.candidate_failure_count,
                    "infrastructure_incident_count": c.infrastructure_incident_count,
                    "inherited_candidate_index": c.inherited_candidate_index,
                    "mean": c.mean,
                    "mean_gatk_runtime_ms": c.mean_gatk_runtime_ms,
                    "objective": c.objective,
                    "observed_count": c.observed_count,
                    "rank": c.rank,
                }
                for c in self.candidates
            ],
            "execution_environment_hash": self.execution_environment_hash,
            "finalist_freeze_sha256": self.finalist_freeze_sha256,
            "member_count": self.member_count,
            "minos_subnet_sha": self.minos_subnet_sha,
            "observation_count": self.observation_count,
            "observations": [o.model_dump(mode="json") for o in self.observations],
            "ordered_ranking": list(self.ordered_ranking),
            "phase_c_closure_sha256": self.phase_c_closure_sha256,
            "phase_d_plan_hash": self.phase_d_plan_hash,
            "schema_version": self.schema_version,
            "scoring_contract_hash": self.scoring_contract_hash,
            "seed_config_hash": self.seed_config_hash,
            "seed_rank": self.seed_rank,
            "selected_config_hash": self.selected_config_hash,
            "selection_interpretation_hash": self.selection_interpretation_hash,
            "selection_interpretation_status": self.selection_interpretation_status,
        }


def compute_phase_d_closure_hash(closure: PhaseDClosure) -> str:
    """The domain-separated identity of one Phase-D closure."""
    return sha256_hex(
        PHASE_D_CLOSURE_DOMAIN.encode("utf-8") + canonical_json_bytes(closure.content())
    )


def _require_frozen_authority(authority: PhaseDAuthority) -> None:
    interpretation = selection_interpretation_content()
    if authority.plan_hash != interpretation["phase_d_plan_hash"]:
        raise PhaseDClosureError(
            f"closure authority plan {authority.plan_hash} is not the frozen Phase-D plan "
            f"{interpretation['phase_d_plan_hash']}"
        )
    if list(authority.ordered_config_hashes) != list(interpretation["ordered_finalists"]):
        raise PhaseDClosureError("the authority's finalists are not the frozen four, or reordered")
    frozen_index = dict(interpretation["inherited_candidate_index"])
    if dict(authority.inherited_candidate_index) != frozen_index:
        raise PhaseDClosureError(
            "the authority's inherited candidate indices are not the frozen Phase-B indices"
        )


def _row_pair(row: Mapping[str, Any]) -> tuple[int, int]:
    return int(row["member_index"]), int(row["config_index"])


def derive_phase_d_observations(
    rows: Sequence[Mapping[str, Any]], *, authority: PhaseDAuthority
) -> tuple[dict[tuple[int, int], BaselineObservation], tuple[ClosureObservation, ...]]:
    """Turn closure rows into the exact forty observations, or refuse.

    Returns the observations keyed by frozen pair, plus their canonical identities ordered by
    ``(member_index, config_index)`` — never by database arrival order.
    """
    _require_frozen_authority(authority)
    frozen_configs = {h: i for i, h in enumerate(authority.ordered_config_hashes)}
    frozen_members = {m.dataset_id: m for m in authority.schedule.members}
    member_index_by_dataset = {m.dataset_id: i for i, m in enumerate(authority.schedule.members)}

    by_pair: dict[tuple[int, int], Mapping[str, Any]] = {}
    environments: set[str] = set()

    for row in rows:
        if str(row["plan_hash"]) != authority.plan_hash:
            raise PhaseDClosureError(
                f"row for plan {row['plan_hash']} is not the frozen Phase-D campaign"
            )
        config_hash = str(row["config_hash"])
        if config_hash not in frozen_configs:
            raise PhaseDClosureError(f"config {config_hash} is not a frozen Phase-D finalist")
        dataset_id = str(row["dataset_id"])
        if dataset_id not in frozen_members:
            raise PhaseDClosureError(f"member {dataset_id} is not a frozen VALIDATION member")
        member = frozen_members[dataset_id]
        if str(row["chromosome"]) != member.chromosome or str(row["round_id"]) != member.round_id:
            raise PhaseDClosureError(f"member {dataset_id} does not match its frozen identity")
        pair = _row_pair(row)
        if pair != (member_index_by_dataset[dataset_id], frozen_configs[config_hash]):
            raise PhaseDClosureError(
                f"row indices {pair} disagree with the frozen identities they carry"
            )

        # A different scoring contract is not this campaign's evidence. Ignored, never averaged.
        contract = row.get("scoring_contract_hash") or row.get(
            "evaluation_failure_scoring_contract_hash"
        )
        if contract is not None and str(contract) != ACCEPTED_SCORING_CONTRACT_HASH:
            continue

        if pair in by_pair:
            raise PhaseDClosureError(
                f"frozen pair {pair} has more than one terminal outcome under the frozen scoring "
                "contract; which one is real is not a question closure may answer by itself"
            )
        by_pair[pair] = row

        environment = row.get("execution_environment_hash") or row.get(
            "execution_failure_environment_hash"
        )
        if environment is not None:
            environments.add(str(environment))

    required = {
        (mi, ci)
        for mi in range(len(authority.schedule.members))
        for ci in range(len(authority.ordered_config_hashes))
    }
    missing = sorted(required - set(by_pair))
    extra = sorted(set(by_pair) - required)
    if missing or extra:
        raise PhaseDClosureError(
            f"the Phase-D matrix is not the frozen cross product: {len(missing)} missing "
            f"{missing[:4]}, {len(extra)} unexpected {extra[:4]}"
        )
    if len(environments) != 1:
        raise PhaseDClosureError(
            f"the forty outcomes bind {len(environments)} execution environments "
            f"{sorted(environments)}; candidates compared across different environments are not "
            "comparable"
        )

    observations: dict[tuple[int, int], BaselineObservation] = {}
    identities: list[ClosureObservation] = []
    for pair in sorted(required):
        row = by_pair[pair]
        observation, identity = _one_observation(row, pair=pair)
        if observation.outcome == "INFRASTRUCTURE_INCIDENT":
            raise PhaseDClosureError(
                f"frozen pair {pair} is an INFRASTRUCTURE_INCIDENT ({observation.failure_code}); "
                "finalists are never ranked over our own defect"
            )
        observations[pair] = observation
        identities.append(identity)
    return observations, tuple(identities)


def _one_observation(
    row: Mapping[str, Any], *, pair: tuple[int, int]
) -> tuple[BaselineObservation, ClosureObservation]:
    """One decided outcome. Never invents a score, never writes zero for a failure."""
    execution_failure = row.get("execution_failure_code")
    evaluation_failure = row.get("evaluation_failure_code")
    admitted = bool(row.get("admitted")) if row.get("admitted") is not None else False
    minos_score = row.get("minos_score")

    if execution_failure is not None:
        failure_code: str | None = str(execution_failure)
        runtime = int(row.get("execution_failure_runtime_ms") or 0)
        admitted, minos_score = False, None
    elif evaluation_failure is not None:
        failure_code = str(evaluation_failure)
        runtime = int(row.get("execution_runtime_ms") or 0)
        admitted, minos_score = False, None
    elif row.get("evaluation_id") is None:
        raise PhaseDClosureError(
            f"frozen pair {pair} has no terminal evaluation; a missing evaluation is neither a "
            "zero nor a failure, and the matrix is simply not complete"
        )
    else:
        failure_code = None
        runtime = int(row.get("execution_runtime_ms") or 0)
        if not admitted:
            # the validator refused the result: a candidate failure with no bounded code.
            minos_score = None

    if failure_code is not None:
        classify_failure_code(failure_code)  # refuses an unknown code rather than guessing

    observation = BaselineObservation(
        config_hash=str(row["config_hash"]),
        dataset_id=str(row["dataset_id"]),
        chromosome=str(row["chromosome"]),
        minos_score=float(minos_score) if admitted and minos_score is not None else None,
        admitted=admitted,
        failure_code=failure_code,
        gatk_runtime_ms=runtime,
    )
    identity = ClosureObservation(
        member_index=pair[0],
        config_index=pair[1],
        config_hash=str(row["config_hash"]),
        dataset_id=str(row["dataset_id"]),
        round_id=str(row["round_id"]),
        chromosome=str(row["chromosome"]),
        job_key=str(row["job_key"]),
        execution_result_id=_opt(row.get("execution_result_id")),
        execution_result_hash=_opt(row.get("execution_result_hash")),
        execution_failure_id=_opt(row.get("execution_failure_id")),
        gatk_runtime_ms=runtime,
        evaluation_id=_opt(row.get("evaluation_id")),
        evaluation_hash=_opt(row.get("evaluation_hash")),
        evaluation_failure_id=_opt(row.get("evaluation_failure_id")),
        scoring_contract_hash=_opt(row.get("scoring_contract_hash")),
        admitted=admitted,
        minos_score=observation.minos_score,
        failure_code=failure_code,
    )
    return observation, identity


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def build_phase_d_closure(
    rows: Sequence[Mapping[str, Any]],
    *,
    authority: PhaseDAuthority,
    baseline_protocol_hash: str,
) -> PhaseDClosure:
    """Aggregate, rank and select — every step delegated to the already-frozen implementation."""
    observations, identities = derive_phase_d_observations(rows, authority=authority)
    interpretation = selection_interpretation_content()

    aggregates: list[CandidateAggregate] = []
    for config_index, config_hash in enumerate(authority.ordered_config_hashes):
        mine = [o for (_, ci), o in sorted(observations.items()) if ci == config_index]
        aggregate = aggregate_candidate(
            config_hash=config_hash,
            observations=mine,
            required_members=authority.required_pairs(),
        )
        if not aggregate.complete:
            raise PhaseDClosureError(
                f"finalist {config_hash} is not complete "
                f"({aggregate.observed_count}/{aggregate.required_count}); a partial candidate is "
                "never finally rankable"
            )
        aggregates.append(aggregate)

    ranked = rank_candidates(aggregates, candidate_index=INHERITED_CANDIDATE_INDEX)
    ordered_hashes = tuple(a.config_hash for a in ranked)
    selected = select_phase_d_baseline_from_ranked_hashes(ordered_hashes)

    closure = PhaseDClosure(
        baseline_protocol_hash=baseline_protocol_hash,
        selection_interpretation_hash=compute_selection_interpretation_hash(),
        selection_interpretation_status=str(interpretation["interpretation_status"]),
        phase_d_plan_hash=authority.plan_hash,
        finalist_freeze_sha256=authority.finalist_freeze_sha256,
        phase_c_closure_sha256=authority.phase_c_closure_sha256,
        execution_environment_hash=str(
            next(
                r["execution_environment_hash"]
                for r in rows
                if r.get("execution_environment_hash") is not None
            )
        ),
        scoring_contract_hash=ACCEPTED_SCORING_CONTRACT_HASH,
        minos_subnet_sha=ACCEPTED_MINOS_SUBNET_COMMIT,
        candidate_count=len(authority.ordered_config_hashes),
        member_count=len(authority.schedule.members),
        observation_count=len(identities),
        candidates=tuple(
            CandidateClosure(
                config_hash=a.config_hash,
                inherited_candidate_index=INHERITED_CANDIDATE_INDEX[a.config_hash],
                observed_count=a.observed_count,
                candidate_failure_count=a.failure_count,
                infrastructure_incident_count=a.infrastructure_incident_count,
                cvar=a.cvar,
                floor=a.floor,
                mean=a.mean,
                failure_rate=a.failure_rate,
                objective=a.objective,
                mean_gatk_runtime_ms=a.mean_gatk_runtime_ms,
                rank=rank,
            )
            for rank, a in enumerate(ranked)
        ),
        observations=identities,
        ordered_ranking=ordered_hashes,
        selected_config_hash=selected,
        seed_config_hash=SEED_CONFIG_HASH,
        seed_rank=ordered_hashes.index(SEED_CONFIG_HASH),
    )
    return closure
