"""Turn a COMPLETE Phase-A screen into the frozen Phase-B design. Pure and deterministic.

Every scientific rule here already exists and is frozen: ``aggregate_candidate`` computes J,
``parameter_impacts`` measures per-dimension movement, ``select_influential_dimensions`` applies
the K=6 rule, ``select_anchors`` picks one best alternative per dimension, and
``build_phase_b_design`` produces the 48-candidate set. This module wires them together and
supplies nothing of its own — no weight, no threshold, no tie-break, no re-implementation.

The one rule it does enforce is **completeness**. Phase-A sensitivity selection reads the screen
as a whole: an impact is a mean over members, and the K=6 cut is a comparison between dimensions.
Running it on a partial screen would let whichever jobs happened to finish first decide which
dimensions Phase B explores — and once Phase B is designed, that choice is not revisited. So an
incomplete screen is refused rather than approximated, and the canary's own score has no
influence on the design until all 195 observations exist.

It selects no baseline, touches no validation or test data, and changes no protocol decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.baseline.objective import (
    BaselineObservation,
    CandidateAggregate,
    aggregate_candidate,
)
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

if TYPE_CHECKING:
    from minos_engine.baseline.design import InfluentialDimension, PhaseBDesign
    from minos_engine.baseline.phase_a_observations import PhaseAObservationSnapshot

__all__ = [
    "PHASE_A_ANALYSIS_DOMAIN",
    "PHASE_A_ANALYSIS_SCHEMA",
    "PhaseAAnalysis",
    "PhaseAAnalysisError",
    "analyze_completed_phase_a",
    "compute_phase_a_analysis_hash",
    "derive_completed_phase_a_analysis",
]

PHASE_A_ANALYSIS_SCHEMA = "l2f2-phase-a-analysis-v1"
#: domain-separated identity of the INPUTS that can move dimensions, anchors or the Phase-B
#: design. Everything Phase B inherits from Phase A is reproducible from this hash.
PHASE_A_ANALYSIS_DOMAIN = "minos:l2f2-phase-a-analysis:v1\n"


class PhaseAAnalysisError(MinosEngineError):
    """The Phase-A screen is not complete or coherent enough to analyse."""


@dataclass(frozen=True)
class PhaseAAnalysis:
    """The deterministic product of a COMPLETE Phase-A screen."""

    aggregates: dict[str, CandidateAggregate]
    impacts: dict[str, float]
    dimensions: tuple[InfluentialDimension, ...]
    anchors: tuple[str, ...]
    design: PhaseBDesign

    @property
    def seed_config_hash(self) -> str:
        return self.design.seed_config_hash


def compute_phase_a_analysis_hash(
    snapshot: PhaseAObservationSnapshot,
    *,
    plan_hash: str,
    protocol_hash: str,
    scoring_contract_hash: str,
    execution_environment_hash: str,
) -> str:
    """The deterministic identity of everything that can move the Phase-B design.

    Binds the frozen plan, the protocol, the scoring contract, the runtime the campaign ran under,
    and the ORDERED observations themselves. ``gatk_runtime_ms`` is included deliberately: anchor
    selection breaks ties on mean GATK runtime, so a screen with identical scores but different
    runtimes can yield different anchors and must therefore be a different identity.

    Database UUIDs, timestamps, filesystem paths, hostnames and worker ids are excluded, so the
    same immutable ledgers re-derive the same hash on any host, at any time.
    """
    content = {
        "schema_version": PHASE_A_ANALYSIS_SCHEMA,
        "plan_hash": plan_hash,
        "baseline_protocol_hash": protocol_hash,
        "scoring_contract_hash": scoring_contract_hash,
        "execution_environment_hash": execution_environment_hash,
        "observation_count": len(snapshot.observations),
        "observations": [
            {
                "config_hash": o.config_hash,
                "dataset_id": o.dataset_id,
                "chromosome": o.chromosome,
                "outcome": o.outcome,
                "admitted": o.admitted,
                "minos_score": o.minos_score,
                "failure_code": o.failure_code,
                "gatk_runtime_ms": o.gatk_runtime_ms,
            }
            # the ledger's own member-major order is the identity's order.
            for o in snapshot.observations
        ],
    }
    return sha256_hex(PHASE_A_ANALYSIS_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def derive_completed_phase_a_analysis(engine: Any) -> tuple[PhaseAAnalysis, str]:
    """THE production Phase-A result boundary: analysis + its identity, from the ledger alone.

    A caller supplies an engine and nothing else — no dimensions, no anchors, no scores, no
    candidate hashes, no plan override, no member list. Everything scientific is recomputed from
    committed authority and the immutable rows, so what Phase B inherits cannot be nominated.

    Refuses an incomplete screen, a screen holding any infrastructure incident (those are our
    failures, and a design chosen over them would be inheriting our own defect), and a screen
    assembled from more than one runtime.
    """
    from minos_engine.baseline.phase_a import build_phase_a_authority
    from minos_engine.baseline.phase_a_observations import (
        PHASE_A_SCORING_CONTRACT,
        load_phase_a_observations,
    )
    from minos_engine.baseline.protocol import build_baseline_protocol

    authority = build_phase_a_authority()
    snapshot = load_phase_a_observations(engine)
    required = authority.plan.logical_job_count
    if len(snapshot.observations) != required:
        raise PhaseAAnalysisError(
            f"Phase-A is not complete: {len(snapshot.observations)} of {required} decided "
            "observations. The Phase-B design is never derived from a partial screen."
        )
    if snapshot.infrastructure_incident_count:
        raise PhaseAAnalysisError(
            f"Phase A holds {snapshot.infrastructure_incident_count} infrastructure incident(s); "
            "those are our failures, not the candidates', and a Phase-B design derived over them "
            "would inherit a defect of ours as a scientific conclusion"
        )
    if snapshot.execution_environment_hash is None:
        raise PhaseAAnalysisError("the completed Phase-A screen carries no runtime identity")

    analysis = analyze_completed_phase_a(snapshot)
    analysis_hash = compute_phase_a_analysis_hash(
        snapshot,
        plan_hash=authority.plan_hash,
        protocol_hash=authority.baseline_protocol_hash,
        scoring_contract_hash=PHASE_A_SCORING_CONTRACT,
        execution_environment_hash=snapshot.execution_environment_hash,
    )
    if authority.baseline_protocol_hash != build_baseline_protocol().protocol_hash:
        raise PhaseAAnalysisError("the Phase-A authority is not bound to the frozen protocol")
    return analysis, analysis_hash


def analyze_completed_phase_a(snapshot: PhaseAObservationSnapshot) -> PhaseAAnalysis:
    """Analyse a COMPLETE Phase-A screen using only the already-frozen rules."""
    from minos_engine.baseline.design import (
        build_phase_b_design,
        dimension_of_alternative,
        parameter_impacts,
        select_anchors,
        select_influential_dimensions,
    )
    from minos_engine.baseline.phase_a import build_phase_a_authority
    from minos_engine.experiments.candidates import generate_accepted_candidate_set

    authority = build_phase_a_authority()
    plan = authority.plan
    required = plan.logical_job_count

    observations = list(snapshot.observations)
    if len(observations) != required:
        raise PhaseAAnalysisError(
            f"Phase-A analysis requires all {required} decided observations, got "
            f"{len(observations)}. Selecting dimensions from a partial screen would let job "
            "completion order decide what Phase B explores."
        )

    candidate_set = generate_accepted_candidate_set()
    configs = list(candidate_set.configs)
    if len(configs) != plan.candidate_count:
        raise PhaseAAnalysisError(
            f"the accepted candidate set has {len(configs)} configs, the frozen plan expects "
            f"{plan.candidate_count}"
        )
    seed = configs[0]
    if seed.config_hash != candidate_set.seed_config_hash:
        raise PhaseAAnalysisError("the accepted candidate set does not lead with its own seed")
    accepted_index = {config.config_hash: index for index, config in enumerate(configs)}

    # the exact member set the objective must aggregate over, from committed authority.
    required_members = _required_members(snapshot, plan)

    by_config: dict[str, list[BaselineObservation]] = {c.config_hash: [] for c in configs}
    for observation in observations:
        bucket = by_config.get(observation.config_hash)
        if bucket is None:
            raise PhaseAAnalysisError(
                f"observation for {observation.config_hash} is not an accepted Phase-A candidate"
            )
        bucket.append(observation)

    aggregates: dict[str, CandidateAggregate] = {}
    for config in configs:
        aggregate = aggregate_candidate(
            config_hash=config.config_hash,
            observations=by_config[config.config_hash],
            required_members=required_members,
        )
        if not aggregate.complete:
            raise PhaseAAnalysisError(
                f"candidate {config.config_hash} observed {aggregate.observed_count} of "
                f"{aggregate.required_count} required members"
            )
        aggregates[config.config_hash] = aggregate

    dimension_by_config = {
        config.config_hash: dimension_of_alternative(config, seed)
        for config in configs
        if config.config_hash != seed.config_hash
    }
    impacts = parameter_impacts(
        observations=observations,
        seed_config_hash=seed.config_hash,
        dimension_by_config=dimension_by_config,
    )
    dimensions = select_influential_dimensions(impacts)
    anchors = select_anchors(
        dimensions=dimensions,
        aggregates=aggregates,
        dimension_by_config=dimension_by_config,
        accepted_index=accepted_index,
    )
    design = build_phase_b_design(dimensions=dimensions, seed=seed, anchor_config_hashes=anchors)
    return PhaseAAnalysis(
        aggregates=aggregates,
        impacts=dict(impacts),
        dimensions=dimensions,
        anchors=anchors,
        design=design,
    )


def _required_members(snapshot: PhaseAObservationSnapshot, plan: Any) -> list[tuple[str, str]]:
    """The frozen ``(dataset_id, chromosome)`` member set, cross-checked against the screen.

    The dataset list comes from the frozen plan; the chromosome each carries is read from the
    observations rather than assumed, and every observation of a member must agree.
    """
    chromosome_of: dict[str, str] = {}
    for observation in snapshot.observations:
        existing = chromosome_of.get(observation.dataset_id)
        if existing is None:
            chromosome_of[observation.dataset_id] = observation.chromosome
        elif existing != observation.chromosome:
            raise PhaseAAnalysisError(
                f"member {observation.dataset_id} is observed as both {existing!r} and "
                f"{observation.chromosome!r}"
            )
    members: list[tuple[str, str]] = []
    for member in plan.members:
        chromosome = chromosome_of.get(member.dataset_id)
        if chromosome is None:
            raise PhaseAAnalysisError(
                f"frozen Phase-A member {member.dataset_id} has no observation at all"
            )
        members.append((member.dataset_id, chromosome))
    if len(members) != plan.train_member_count:
        raise PhaseAAnalysisError(
            f"the frozen plan has {plan.train_member_count} members, resolved {len(members)}"
        )
    return members
