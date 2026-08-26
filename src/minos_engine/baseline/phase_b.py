"""The Phase-B execution authority: 48 configurations × 10 TRAIN members, derived, never chosen.

Phase A screened one knob at a time and told us which six actually move the score. Phase B
explores those six together, on twice the data. Everything scientific here is a CONSEQUENCE of the
completed Phase-A ledger and the already-frozen protocol — a caller supplies an engine and gets an
authority back; it cannot nominate a dimension, an anchor, a configuration, a member or a plan.

Three things are worth stating plainly because they are easy to get wrong.

**The candidate set is a RESULT, not a generation policy.** Phase A's candidate-set identity
describes a one-at-a-time probe of the accepted parameter space. Phase B's 48 configurations are
not that: they are the output of an analysis of observed data. Reusing Phase A's identity would
claim these configurations were generated the way those were. They get their own domain, bound to
the Phase-A analysis they descend from, and the frozen protocol hash is untouched — this is a
derived result under that protocol, not a change to it.

**Failed anchors stay.** Two of the six anchors are Phase-A alternatives that failed on every
member. That is not a defect to repair: impact measures SENSITIVITY, and a knob whose alternative
destroys the score is exactly a knob worth exploring carefully. Dropping those anchors, requiring
an anchor to beat the seed, or narrowing their domains would all be choosing the design after
seeing the data, which is what freezing the protocol before the first score exists prevents.

**The members are the frozen schedule's first two balanced batches**, in schedule order: ten
members, one per chromosome per batch. Plan-local ``member_index`` is 0..9; each member's identity
is taken verbatim from the accepted 50-member TRAIN plan, so the source feature-matrix ordinals
follow the accepted closure rather than the plan-local renumbering.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.baseline.design import PhaseBDesign
from minos_engine.callers.gatk.config import CanonicalConfig
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.experiments.plan import ExperimentPlan

__all__ = [
    "PHASE_B_ANCHOR_COUNT",
    "PHASE_B_BATCH_COUNT",
    "PHASE_B_BATCH_SIZE",
    "PHASE_B_CANDIDATE_COUNT",
    "PHASE_B_CANDIDATE_SET_DOMAIN",
    "PHASE_B_CANDIDATE_SET_SCHEMA",
    "PHASE_B_LHS_COUNT",
    "PHASE_B_LOGICAL_JOB_COUNT",
    "PHASE_B_MEMBER_COUNT",
    "PHASE_B_PHASE",
    "PhaseBAuthority",
    "PhaseBError",
    "build_l2f2_phase_b_authority",
    "compute_phase_b_candidate_set_hash",
    "verify_phase_b_candidates",
]

PHASE_B_PHASE = "PHASE_B"
PHASE_B_BATCH_COUNT = 2
PHASE_B_BATCH_SIZE = 5
PHASE_B_MEMBER_COUNT = PHASE_B_BATCH_COUNT * PHASE_B_BATCH_SIZE  # 10
PHASE_B_CANDIDATE_COUNT = 48
PHASE_B_ANCHOR_COUNT = 6
PHASE_B_LHS_COUNT = 41
PHASE_B_LOGICAL_JOB_COUNT = PHASE_B_MEMBER_COUNT * PHASE_B_CANDIDATE_COUNT  # 480

PHASE_B_CANDIDATE_SET_SCHEMA = "l2f2-phase-b-candidate-set-v1"
PHASE_B_CANDIDATE_SET_DOMAIN = "minos:l2f2-phase-b-candidate-set:v1\n"

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class PhaseBError(MinosEngineError):
    """The Phase-B authority cannot be derived from the completed Phase-A ledger."""


class PhaseBAuthority(BaseModel):
    """The complete Phase-B execution authority. Every field is derived, none is supplied."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    baseline_protocol_hash: str = Field(min_length=64, max_length=64)
    source_phase_a_plan_hash: str = Field(min_length=64, max_length=64)
    phase_a_analysis_hash: str = Field(min_length=64, max_length=64)
    phase_b_candidate_set_hash: str = Field(min_length=64, max_length=64)
    #: the runtime the completed Phase-A campaign ran under. Phase B must match it: a baseline
    #: search whose two phases ran on different runtimes is not one experiment.
    execution_environment_hash: str = Field(min_length=64, max_length=64)
    seed_config_hash: str = Field(min_length=64, max_length=64)
    train_schedule_manifest_sha256: str = Field(min_length=64, max_length=64)
    split_manifest_sha256: str = Field(min_length=64, max_length=64)
    plan: ExperimentPlan
    configs: tuple[CanonicalConfig, ...]
    design: PhaseBDesign
    #: the chromosome of each plan-local member, in member order. Carried explicitly because the
    #: objective's required member set is ``(dataset_id, chromosome)`` pairs.
    member_chromosomes: tuple[str, ...]

    @property
    def plan_hash(self) -> str:
        return str(self.plan.plan_hash)

    @property
    def anchor_config_hashes(self) -> tuple[str, ...]:
        return tuple(self.design.anchor_config_hashes)

    def required_pairs(self, batch_count: int = PHASE_B_BATCH_COUNT) -> tuple[tuple[str, str], ...]:
        """``(dataset_id, chromosome)`` for the first ``batch_count`` batches, in plan order."""
        if not 1 <= batch_count <= PHASE_B_BATCH_COUNT:
            raise PhaseBError(f"batch_count {batch_count} outside 1..{PHASE_B_BATCH_COUNT}")
        limit = batch_count * PHASE_B_BATCH_SIZE
        return tuple(
            (m.dataset_id, chromosome)
            for m, chromosome in zip(self.plan.members, self.member_chromosomes, strict=True)
        )[:limit]

    def batch_members(self, batch_index: int) -> tuple[int, ...]:
        """The plan-local member indices of ONE balanced batch."""
        if batch_index not in range(PHASE_B_BATCH_COUNT):
            raise PhaseBError(f"batch_index {batch_index} outside 0..{PHASE_B_BATCH_COUNT - 1}")
        start = batch_index * PHASE_B_BATCH_SIZE
        return tuple(range(start, start + PHASE_B_BATCH_SIZE))


def compute_phase_b_candidate_set_hash(
    *,
    protocol_hash: str,
    source_phase_a_plan_hash: str,
    phase_a_analysis_hash: str,
    parameter_space_hash: str,
    experiment_parameter_policy_hash: str,
    design: PhaseBDesign,
) -> str:
    """The identity of the 48-candidate Phase-B set, as a RESULT of the Phase-A analysis.

    It binds the protocol it was produced under, the Phase-A plan and analysis it descends from,
    the parameter space and policy the configurations are canonical in, and the design itself —
    dimensions with their measured impacts, the anchors, and every ordered config hash. Two
    campaigns that screened differently cannot collide on this identity even if they happened to
    land on the same 48 configurations.
    """
    content = {
        "schema_version": PHASE_B_CANDIDATE_SET_SCHEMA,
        "baseline_protocol_hash": protocol_hash,
        "source_phase_a_plan_hash": source_phase_a_plan_hash,
        "phase_a_analysis_hash": phase_a_analysis_hash,
        "parameter_space_hash": parameter_space_hash,
        "experiment_parameter_policy_hash": experiment_parameter_policy_hash,
        "seed_config_hash": design.seed_config_hash,
        "influential_dimensions": [
            {"name": d.name, "impact": d.impact, "live_parameter_index": d.live_parameter_index}
            for d in design.dimensions
        ],
        "anchor_config_hashes": list(design.anchor_config_hashes),
        "ordered_config_hashes": list(design.ordered_config_hashes),
        "candidate_count": PHASE_B_CANDIDATE_COUNT,
    }
    return sha256_hex(PHASE_B_CANDIDATE_SET_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def verify_phase_b_candidates(
    configs: tuple[CanonicalConfig, ...], *, design: PhaseBDesign, seed: CanonicalConfig
) -> None:
    """Fail closed unless the 48 configurations are exactly what the design says they are.

    Every configuration must recanonicalize to itself under the committed live-GATK domain, bind
    the accepted parameter space, and be unique. Composition is checked positionally: seed first,
    the six anchors at 1..6 in selected-dimension order, the 41 LHS configurations after them.

    Each LHS configuration may differ from the seed ONLY in the six selected dimensions — it need
    not differ in all six, because the frozen design maps a stratum midpoint per dimension and a
    midpoint may legitimately land on the seed's own value.
    """
    from minos_engine.experiments.gatk_live_space import canonicalize_live_gatk_config

    if len(configs) != PHASE_B_CANDIDATE_COUNT:
        raise PhaseBError(f"Phase B takes exactly {PHASE_B_CANDIDATE_COUNT} configs")
    hashes = tuple(c.config_hash for c in configs)
    if hashes != design.ordered_config_hashes:
        raise PhaseBError("the configurations do not reproduce the design's ordered hashes")
    if len(set(hashes)) != PHASE_B_CANDIDATE_COUNT:
        raise PhaseBError("the Phase-B candidate set contains a duplicate configuration")
    if hashes[0] != design.seed_config_hash:
        raise PhaseBError("position 0 of the Phase-B design is not the seed")
    if hashes[1 : 1 + PHASE_B_ANCHOR_COUNT] != design.anchor_config_hashes:
        raise PhaseBError("positions 1..6 are not the selected anchors in dimension order")

    selected = {d.name for d in design.dimensions}
    space_hash = seed.parameter_space_hash
    for position, config in enumerate(configs):
        recanonical = canonicalize_live_gatk_config(dict(config.effective_config))
        if recanonical.config_hash != config.config_hash:
            raise PhaseBError(f"Phase-B config {position} does not recanonicalize to itself")
        if config.parameter_space_hash != space_hash:
            raise PhaseBError(
                f"Phase-B config {position} binds parameter space {config.parameter_space_hash}, "
                f"the accepted space is {space_hash}"
            )
        if position >= 1 + PHASE_B_ANCHOR_COUNT:
            moved = {
                name
                for name, value in config.effective_config.items()
                if seed.effective_config.get(name) != value
            }
            outside = sorted(moved - selected)
            if outside:
                raise PhaseBError(
                    f"LHS config {position} moves {outside}, which are not selected dimensions"
                )


def build_l2f2_phase_b_authority(engine: Any) -> PhaseBAuthority:
    """THE production Phase-B authority. Derived from the completed Phase-A ledger alone.

    Reuses the accepted E5 prerequisite closure through the accepted plan builder, so the members
    are projected from the same verified snapshot / TRAIN matrix / feature registry the Phase-A
    plan was built from — never assembled from database rows.
    """
    from minos_engine.baseline.design import phase_b_candidate_configs
    from minos_engine.baseline.phase_a import build_phase_a_authority
    from minos_engine.baseline.phase_a_analysis import derive_completed_phase_a_analysis
    from minos_engine.baseline.schedule import build_train_schedule, split_manifest_sha256
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
    from minos_engine.experiments.candidates import generate_accepted_candidate_set
    from minos_engine.experiments.plan import (
        ExperimentPlanMember,
        _assemble_experiment_plan,
    )

    analysis, analysis_hash = derive_completed_phase_a_analysis(engine)
    phase_a = build_phase_a_authority()
    accepted = build_accepted_experiment_plan()  # re-proves the accepted E5 closure
    schedule = build_train_schedule()
    candidate_set = generate_accepted_candidate_set()
    seed = candidate_set.configs[0]
    if seed.config_hash != analysis.design.seed_config_hash:
        raise PhaseBError("the Phase-B design's seed is not the accepted seed configuration")

    scheduled = schedule.phase_members(PHASE_B_BATCH_COUNT)
    if len(scheduled) != PHASE_B_MEMBER_COUNT:
        raise PhaseBError(
            f"the first {PHASE_B_BATCH_COUNT} TRAIN batches hold {len(scheduled)} members, "
            f"expected {PHASE_B_MEMBER_COUNT}"
        )
    by_dataset = {m.dataset_id: m for m in accepted.members}
    ordered: list[ExperimentPlanMember] = []
    for index, member in enumerate(scheduled):
        source = by_dataset.get(member.dataset_id)
        if source is None:
            raise PhaseBError(
                f"TRAIN schedule member {member.dataset_id} is absent from the accepted plan"
            )
        ordered.append(
            ExperimentPlanMember(
                dataset_id=source.dataset_id,
                profile_id=source.profile_id,
                content_hash=source.content_hash,
                feature_values_hash=source.feature_values_hash,
                vector_hash=source.vector_hash,
                member_index=index,
            )
        )

    by_hash = {c.config_hash: c for c in candidate_set.configs}
    missing = [h for h in analysis.design.anchor_config_hashes if h not in by_hash]
    if missing:
        raise PhaseBError(f"anchors {missing} are not accepted Phase-A candidates")
    configs = phase_b_candidate_configs(
        design=analysis.design,
        seed=seed,
        anchors={h: by_hash[h] for h in analysis.design.anchor_config_hashes},
    )
    verify_phase_b_candidates(configs, design=analysis.design, seed=seed)

    phase_b_set = _phase_b_candidate_set(
        candidate_set,
        configs,
        analysis.design,
        protocol_hash=phase_a.baseline_protocol_hash,
        source_phase_a_plan_hash=phase_a.plan_hash,
        phase_a_analysis_hash=analysis_hash,
    )
    plan = _assemble_experiment_plan(
        epoch=accepted.epoch,
        snapshot_hash=accepted.snapshot_hash,
        split_manifest_hash=accepted.split_manifest_hash,
        registry_snapshot_hash=accepted.registry_snapshot_hash,
        train_matrix_hash=accepted.train_matrix_hash,
        train_feature_view_hash=accepted.train_feature_view_hash,
        feature_set_hash=accepted.feature_set_hash,
        feature_registry_hash=accepted.feature_registry_hash,
        candidate_set=phase_b_set,
        ordered_members=ordered,
    )
    if plan.partition != "train":
        raise PhaseBError("the Phase-B plan is not a TRAIN plan")
    if plan.logical_job_count != PHASE_B_LOGICAL_JOB_COUNT:
        raise PhaseBError(
            f"Phase-B plan has {plan.logical_job_count} logical jobs, "
            f"expected {PHASE_B_LOGICAL_JOB_COUNT}"
        )
    if plan.plan_hash == phase_a.plan_hash:
        raise PhaseBError("the Phase-B plan collides with the Phase-A plan")

    snapshot_environment = _phase_a_environment(engine)
    return PhaseBAuthority(
        baseline_protocol_hash=phase_a.baseline_protocol_hash,
        source_phase_a_plan_hash=phase_a.plan_hash,
        phase_a_analysis_hash=analysis_hash,
        phase_b_candidate_set_hash=phase_b_set.candidate_set_hash,
        execution_environment_hash=snapshot_environment,
        seed_config_hash=seed.config_hash,
        train_schedule_manifest_sha256=phase_a.train_schedule_manifest_sha256,
        split_manifest_sha256=split_manifest_sha256(),
        plan=plan,
        configs=configs,
        design=analysis.design,
        member_chromosomes=tuple(m.chromosome for m in scheduled),
    )


def _phase_a_environment(engine: Any) -> str:
    from minos_engine.baseline.phase_a_observations import load_phase_a_observations

    snapshot = load_phase_a_observations(engine)
    if snapshot.execution_environment_hash is None:
        raise PhaseBError("the completed Phase-A campaign carries no runtime identity")
    return str(snapshot.execution_environment_hash)


def _phase_b_candidate_set(
    accepted_set: Any,
    configs: Any,
    design: Any,
    *,
    protocol_hash: str,
    source_phase_a_plan_hash: str,
    phase_a_analysis_hash: str,
) -> Any:
    """The Phase-B candidate set object, carrying its OWN result identity.

    It reuses the accepted policy object — the configurations really are canonical in that
    parameter space — but replaces the identity, because these 48 were analysed into existence,
    not generated by the one-at-a-time probe that Phase A's identity describes.
    """
    from dataclasses import replace

    hashes = tuple(c.config_hash for c in configs)
    candidate_set_hash = compute_phase_b_candidate_set_hash(
        protocol_hash=protocol_hash,
        source_phase_a_plan_hash=source_phase_a_plan_hash,
        phase_a_analysis_hash=phase_a_analysis_hash,
        parameter_space_hash=accepted_set.policy.parameter_space_hash,
        experiment_parameter_policy_hash=accepted_set.policy.experiment_parameter_policy_hash,
        design=design,
    )
    return replace(
        accepted_set,
        configs=tuple(configs),
        ordered_config_hashes=hashes,
        candidate_count=len(hashes),
        candidate_set_hash=candidate_set_hash,
        skipped=(),
    )
