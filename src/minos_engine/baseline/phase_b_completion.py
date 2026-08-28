"""The COMPLETE Phase-B TRAIN result and its identity — the provenance Phase C descends from.

Phase C is not a new experiment so much as a consequence: ten configurations, chosen by the frozen
rules from a finished 480-observation screen. For that descent to be auditable, the screen itself
needs an identity — a hash that says *these* 480 outcomes, under *this* plan, candidate set,
parameter space and runtime, produced *these* ten. Recomputing it from the immutable ledger must
give the same answer forever; recomputing it from a screen that moved must not.

The identity binds only what the science needs to be reproducible: for each observation, the
configuration, the member, the chromosome, the outcome, the admitted score, the failure code and
the GATK runtime — everything ``aggregate_candidate``, the frozen tie-break and the promotion rule
actually consume. Database UUIDs, timestamps, worker ids, hostnames and paths are deliberately
absent: two stores that ran the same science must agree, and the same store must not disagree with
itself because a row was inserted a second later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from minos_engine.baseline.objective import BaselineObservation, CandidateAggregate

__all__ = [
    "PHASE_B_COMPLETION_DOMAIN",
    "PHASE_B_COMPLETION_SCHEMA",
    "PhaseBCompletionError",
    "PhaseBCompletionResult",
    "compute_phase_b_completion_hash",
    "derive_completed_phase_b_result",
]

PHASE_B_COMPLETION_SCHEMA = "l2f2-phase-b-completion-v1"
PHASE_B_COMPLETION_DOMAIN = "minos:l2f2-phase-b-completion:v1\n"


class PhaseBCompletionError(MinosEngineError):
    """The Phase-B screen is not in a state a completed result may be derived from."""


@dataclass(frozen=True)
class PhaseBCompletionResult:
    """A finished Phase-B screen: every aggregate, the full ranking, and who was promoted."""

    aggregates: dict[str, CandidateAggregate]
    ranking: tuple[str, ...]
    selected_config_hashes: tuple[str, ...]
    seed_config_hash: str
    plan_hash: str
    candidate_set_hash: str
    parameter_space_hash: str
    execution_environment_hash: str
    observation_count: int
    completion_hash: str


def _observation_content(observation: BaselineObservation) -> dict[str, Any]:
    """Exactly the values the frozen aggregation, ranking and promotion consume. Nothing else."""
    return {
        "config_hash": observation.config_hash,
        "dataset_id": observation.dataset_id,
        "chromosome": observation.chromosome,
        "outcome": observation.outcome,
        "admitted": observation.admitted,
        "minos_score": observation.minos_score,
        "failure_code": observation.failure_code,
        "gatk_runtime_ms": observation.gatk_runtime_ms,
    }


def compute_phase_b_completion_hash(
    observations: tuple[BaselineObservation, ...],
    *,
    protocol_hash: str,
    plan_hash: str,
    candidate_set_hash: str,
    parameter_space_hash: str,
    execution_environment_hash: str,
) -> str:
    """The domain-separated identity of ONE completed Phase-B screen.

    Observations are ordered by ``(config_hash, dataset_id)`` rather than by insertion, so the
    identity describes the screen and not the order a particular worker happened to finish it in.
    """
    ordered = sorted(
        (_observation_content(o) for o in observations),
        key=lambda row: (row["config_hash"], row["dataset_id"]),
    )
    content = {
        "schema_version": PHASE_B_COMPLETION_SCHEMA,
        "baseline_protocol_hash": protocol_hash,
        "phase_b_plan_hash": plan_hash,
        "phase_b_candidate_set_hash": candidate_set_hash,
        "parameter_space_hash": parameter_space_hash,
        "execution_environment_hash": execution_environment_hash,
        "observation_count": len(ordered),
        "observations": ordered,
    }
    return sha256_hex(PHASE_B_COMPLETION_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def derive_completed_phase_b_result(engine: Engine) -> PhaseBCompletionResult:
    """Derive the COMPLETE Phase-B result from the ledger. Nothing is supplied by a caller.

    Every precondition is checked before anything is computed, because a promotion derived from a
    screen that is merely nearly finished is not a promotion — it is a guess with a hash on it.
    """
    from minos_engine.baseline.objective import aggregate_candidate, tie_break_key
    from minos_engine.baseline.phase_b import (
        PHASE_B_CANDIDATE_COUNT,
        PHASE_B_LOGICAL_JOB_COUNT,
        PHASE_B_MEMBER_COUNT,
        build_l2f2_phase_b_authority,
    )
    from minos_engine.baseline.phase_b_observations import load_phase_b_observations
    from minos_engine.baseline.racing import PHASE_B_SURVIVOR_COUNT
    from minos_engine.storage.l2f2_phase_b_control import select_l2f2_phase_c_candidates

    authority = build_l2f2_phase_b_authority(engine)
    snapshot = load_phase_b_observations(engine, authority=authority)
    observations = snapshot.observations

    if snapshot.infrastructure_incident_count:
        raise PhaseBCompletionError(
            f"Phase B holds {snapshot.infrastructure_incident_count} infrastructure incident(s); "
            "a promotion made over our own failures would carry them into Phase C"
        )
    if len(observations) != PHASE_B_LOGICAL_JOB_COUNT:
        raise PhaseBCompletionError(
            f"Phase B has {len(observations)} of {PHASE_B_LOGICAL_JOB_COUNT} decided "
            "observations; the screen is not complete"
        )

    required = list(authority.required_pairs())
    by_config: dict[str, list[BaselineObservation]] = {
        config_hash: [] for config_hash in authority.design.ordered_config_hashes
    }
    for observation in observations:
        if observation.config_hash not in by_config:
            raise PhaseBCompletionError(
                f"observation for {observation.config_hash} is not a Phase-B candidate"
            )
        by_config[observation.config_hash].append(observation)

    if len(by_config) != PHASE_B_CANDIDATE_COUNT:
        raise PhaseBCompletionError(
            f"Phase B holds {len(by_config)} candidates, expected {PHASE_B_CANDIDATE_COUNT}"
        )
    short = sorted(h for h, obs in by_config.items() if len(obs) != PHASE_B_MEMBER_COUNT)
    if short:
        raise PhaseBCompletionError(
            f"{len(short)} candidate(s) lack all {PHASE_B_MEMBER_COUNT} member observations"
        )

    aggregates = {
        config_hash: aggregate_candidate(
            config_hash=config_hash, observations=obs, required_members=required
        )
        for config_hash, obs in by_config.items()
    }
    incomplete = sorted(h for h, a in aggregates.items() if not a.complete)
    if incomplete:  # pragma: no cover - the member check above already forces completeness
        raise PhaseBCompletionError(f"{len(incomplete)} aggregate(s) are incomplete")

    index = authority.design.candidate_index
    ranking = tuple(
        a.config_hash
        for a in sorted(
            aggregates.values(),
            key=lambda a: tie_break_key(a, candidate_index=index[a.config_hash]),
        )
    )
    selected = select_l2f2_phase_c_candidates(engine)
    if len(selected) != PHASE_B_SURVIVOR_COUNT or len(set(selected)) != len(selected):
        raise PhaseBCompletionError(
            f"the promotion returned {len(selected)} configurations, expected "
            f"{PHASE_B_SURVIVOR_COUNT} distinct ones"
        )
    if authority.seed_config_hash not in selected:
        raise PhaseBCompletionError("the seed is absent from the promotion")

    return PhaseBCompletionResult(
        aggregates=aggregates,
        ranking=ranking,
        selected_config_hashes=selected,
        seed_config_hash=authority.seed_config_hash,
        plan_hash=authority.plan_hash,
        candidate_set_hash=authority.phase_b_candidate_set_hash,
        parameter_space_hash=authority.plan.parameter_space_hash,
        execution_environment_hash=authority.execution_environment_hash,
        observation_count=len(observations),
        completion_hash=compute_phase_b_completion_hash(
            observations,
            protocol_hash=authority.baseline_protocol_hash,
            plan_hash=authority.plan_hash,
            candidate_set_hash=authority.phase_b_candidate_set_hash,
            parameter_space_hash=authority.plan.parameter_space_hash,
            execution_environment_hash=authority.execution_environment_hash,
        ),
    )
