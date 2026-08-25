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
from minos_engine.common.errors import MinosEngineError

if TYPE_CHECKING:
    from minos_engine.baseline.design import InfluentialDimension, PhaseBDesign
    from minos_engine.baseline.phase_a_observations import PhaseAObservationSnapshot

__all__ = [
    "PhaseAAnalysis",
    "PhaseAAnalysisError",
    "analyze_completed_phase_a",
]


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
