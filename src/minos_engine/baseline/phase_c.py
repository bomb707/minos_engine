"""The Phase-C execution authority: 10 promoted configurations × 50 TRAIN members.

Phase C confirms, on the whole TRAIN partition, the ten configurations a finished Phase-B screen
promoted. Nothing here is designed: the configurations ARE Phase-B configurations, the members ARE
the committed TRAIN schedule, and the promotion ALREADY happened under the frozen rules. A caller
supplies an engine and gets an authority back.

**Two indexes, and they are not interchangeable.** The plan's own ``config_index`` runs 0..9 and
records where a configuration sits in the Phase-C plan — bookkeeping. The frozen tie-break's third
key is something else entirely: each candidate's ORIGINAL index in the Phase-B design, a number in
0..47, fixed before any Phase-B score existed. Renumbering the promoted ten 0..9 would invent an
ordering *after* observing outcomes, and a rule chosen after the results is not a pre-registered
rule. :func:`~minos_engine.baseline.design.phase_c_inherited_candidate_index` is the only way to
obtain it, and both are bound into the candidate-set identity so the distinction is durable.

**Racing spends less than the ceiling.** 500 pairs is a budget, not a quota: ten balanced batches,
and after each complete one the frozen rule eliminates whoever can no longer reach the top four. An
eliminated candidate correctly stops at the observations it has; it is not missing, not failed, and
never resurrected. The seed is never eliminated.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.callers.gatk.config import CanonicalConfig
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.experiments.plan import ExperimentPlan

__all__ = [
    "PHASE_C_BATCH_COUNT",
    "PHASE_C_BATCH_SIZE",
    "PHASE_C_CANDIDATE_COUNT",
    "PHASE_C_CANDIDATE_SET_DOMAIN",
    "PHASE_C_CANDIDATE_SET_SCHEMA",
    "PHASE_C_LOGICAL_JOB_BUDGET",
    "PHASE_C_MEMBER_COUNT",
    "PHASE_C_PHASE",
    "PhaseCAuthority",
    "PhaseCError",
    "build_l2f2_phase_c_authority",
    "compute_phase_c_candidate_set_hash",
]

PHASE_C_PHASE = "PHASE_C"
PHASE_C_BATCH_COUNT = 10
PHASE_C_BATCH_SIZE = 5
PHASE_C_MEMBER_COUNT = PHASE_C_BATCH_COUNT * PHASE_C_BATCH_SIZE  # 50
PHASE_C_CANDIDATE_COUNT = 10
#: a CEILING, never a quota: racing may only reduce what is actually spent.
PHASE_C_LOGICAL_JOB_BUDGET = PHASE_C_CANDIDATE_COUNT * PHASE_C_MEMBER_COUNT  # 500

PHASE_C_CANDIDATE_SET_SCHEMA = "l2f2-phase-c-candidate-set-v1"
PHASE_C_CANDIDATE_SET_DOMAIN = "minos:l2f2-phase-c-candidate-set:v1\n"


class PhaseCError(MinosEngineError):
    """The Phase-C authority cannot be derived from the completed Phase-B result."""


class PhaseCAuthority(BaseModel):
    """The complete Phase-C execution authority. Every field is derived, none is supplied."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    baseline_protocol_hash: str = Field(min_length=64, max_length=64)
    source_phase_b_plan_hash: str = Field(min_length=64, max_length=64)
    phase_b_completion_hash: str = Field(min_length=64, max_length=64)
    phase_c_candidate_set_hash: str = Field(min_length=64, max_length=64)
    execution_environment_hash: str = Field(min_length=64, max_length=64)
    seed_config_hash: str = Field(min_length=64, max_length=64)
    train_schedule_manifest_sha256: str = Field(min_length=64, max_length=64)
    split_manifest_sha256: str = Field(min_length=64, max_length=64)
    plan: ExperimentPlan
    configs: tuple[CanonicalConfig, ...]
    #: the promoted order — plan-local positions 0..9. Bookkeeping, never a tie-break.
    ordered_config_hashes: tuple[str, ...]
    #: THE scientific tie-break index: each candidate's ORIGINAL Phase-B design position, 0..47.
    inherited_candidate_index: dict[str, int]
    #: the chromosome of each plan-local member, in member order.
    member_chromosomes: tuple[str, ...]

    @property
    def plan_hash(self) -> str:
        return str(self.plan.plan_hash)

    @property
    def phase(self) -> str:
        """Which execution phase this authority IS. Carried so no caller ever names a phase."""
        return PHASE_C_PHASE

    def required_pairs(self, batch_count: int = PHASE_C_BATCH_COUNT) -> tuple[tuple[str, str], ...]:
        """``(dataset_id, chromosome)`` for the first ``batch_count`` batches, in plan order."""
        if not 1 <= batch_count <= PHASE_C_BATCH_COUNT:
            raise PhaseCError(f"batch_count {batch_count} outside 1..{PHASE_C_BATCH_COUNT}")
        limit = batch_count * PHASE_C_BATCH_SIZE
        return tuple(
            (m.dataset_id, chromosome)
            for m, chromosome in zip(self.plan.members, self.member_chromosomes, strict=True)
        )[:limit]

    def batch_members(self, batch_index: int) -> tuple[int, ...]:
        """The plan-local member indices of ONE balanced batch."""
        if batch_index not in range(PHASE_C_BATCH_COUNT):
            raise PhaseCError(f"batch_index {batch_index} outside 0..{PHASE_C_BATCH_COUNT - 1}")
        start = batch_index * PHASE_C_BATCH_SIZE
        return tuple(range(start, start + PHASE_C_BATCH_SIZE))


def compute_phase_c_candidate_set_hash(
    *,
    protocol_hash: str,
    source_phase_b_plan_hash: str,
    phase_b_completion_hash: str,
    parameter_space_hash: str,
    experiment_parameter_policy_hash: str,
    ordered_config_hashes: tuple[str, ...],
    inherited_candidate_index: dict[str, int],
    seed_config_hash: str,
) -> str:
    """The identity of the ten promoted configurations, as a RESULT of a finished Phase B.

    It binds BOTH indexes on purpose. The ordered hashes fix the promotion order; the inherited
    map fixes the scientific tie-break index each candidate carries out of the Phase-B design.
    Two campaigns that promoted the same ten configurations from different screens cannot collide,
    and a later reader cannot mistake one numbering for the other.
    """
    content = {
        "schema_version": PHASE_C_CANDIDATE_SET_SCHEMA,
        "baseline_protocol_hash": protocol_hash,
        "source_phase_b_plan_hash": source_phase_b_plan_hash,
        "phase_b_completion_hash": phase_b_completion_hash,
        "parameter_space_hash": parameter_space_hash,
        "experiment_parameter_policy_hash": experiment_parameter_policy_hash,
        "seed_config_hash": seed_config_hash,
        "candidate_count": PHASE_C_CANDIDATE_COUNT,
        "ordered_config_hashes": list(ordered_config_hashes),
        "inherited_phase_b_candidate_index": [
            {"config_hash": h, "phase_b_candidate_index": inherited_candidate_index[h]}
            for h in ordered_config_hashes
        ],
    }
    return sha256_hex(PHASE_C_CANDIDATE_SET_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def _phase_c_candidate_set(
    accepted_set: Any, configs: tuple[CanonicalConfig, ...], *, candidate_set_hash: str
) -> Any:
    """The accepted candidate-set object, re-pointed at exactly the ten promoted configs.

    It reuses the accepted policy — these ten really are canonical in that parameter space — but
    carries its own identity, because a promotion is a different kind of object from a design.
    """
    from dataclasses import replace

    hashes = tuple(c.config_hash for c in configs)
    return replace(
        accepted_set,
        configs=tuple(configs),
        ordered_config_hashes=hashes,
        candidate_count=len(hashes),
        candidate_set_hash=candidate_set_hash,
        skipped=(),
    )


def build_l2f2_phase_c_authority(engine: Any) -> PhaseCAuthority:
    """Derive Phase C from the COMPLETED Phase-B ledger. Fully deterministic, no overrides.

    Every scientific choice was already made and is only re-derived here: which ten (the frozen
    promotion), on which fifty members (the committed TRAIN schedule, all ten balanced batches),
    under which runtime (the one the Phase-B screen ran under).
    """
    from minos_engine.baseline.design import phase_c_inherited_candidate_index
    from minos_engine.baseline.phase_a import build_phase_a_authority
    from minos_engine.baseline.phase_b import build_l2f2_phase_b_authority
    from minos_engine.baseline.phase_b_completion import derive_completed_phase_b_result
    from minos_engine.baseline.schedule import build_train_schedule, split_manifest_sha256
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
    from minos_engine.experiments.candidates import generate_accepted_candidate_set
    from minos_engine.experiments.gatk_live_space import canonicalize_live_gatk_config
    from minos_engine.experiments.plan import ExperimentPlanMember, _assemble_experiment_plan

    completed = derive_completed_phase_b_result(engine)
    phase_b = build_l2f2_phase_b_authority(engine)
    phase_a = build_phase_a_authority()
    accepted = build_accepted_experiment_plan()  # re-proves the accepted E5 closure
    schedule = build_train_schedule()

    selected = completed.selected_config_hashes
    inherited = phase_c_inherited_candidate_index(phase_b.design, selected)

    by_hash = {c.config_hash: c for c in phase_b.configs}
    missing = [h for h in selected if h not in by_hash]
    if missing:
        raise PhaseCError(f"promoted configuration(s) {missing} are not Phase-B configurations")
    configs = tuple(by_hash[h] for h in selected)
    for position, config in enumerate(configs):
        recanonical = canonicalize_live_gatk_config(dict(config.effective_config))
        if recanonical.config_hash != config.config_hash:
            raise PhaseCError(f"Phase-C config {position} does not recanonicalize to itself")
        if config.parameter_space_hash != phase_b.plan.parameter_space_hash:
            raise PhaseCError(f"Phase-C config {position} binds a different parameter space")
    if completed.seed_config_hash not in selected:
        raise PhaseCError("the accepted seed is absent from the promoted set")

    scheduled = schedule.phase_members(PHASE_C_BATCH_COUNT)
    if len(scheduled) != PHASE_C_MEMBER_COUNT:
        raise PhaseCError(
            f"the TRAIN schedule holds {len(scheduled)} members across "
            f"{PHASE_C_BATCH_COUNT} batches, expected {PHASE_C_MEMBER_COUNT}"
        )
    by_dataset = {m.dataset_id: m for m in accepted.members}
    ordered_members: list[ExperimentPlanMember] = []
    for index, member in enumerate(scheduled):
        source = by_dataset.get(member.dataset_id)
        if source is None:
            raise PhaseCError(
                f"TRAIN schedule member {member.dataset_id} is absent from the accepted plan"
            )
        ordered_members.append(
            ExperimentPlanMember(
                dataset_id=source.dataset_id,
                profile_id=source.profile_id,
                content_hash=source.content_hash,
                feature_values_hash=source.feature_values_hash,
                vector_hash=source.vector_hash,
                member_index=index,
            )
        )

    accepted_set = generate_accepted_candidate_set()
    candidate_set_hash = compute_phase_c_candidate_set_hash(
        protocol_hash=phase_a.baseline_protocol_hash,
        source_phase_b_plan_hash=completed.plan_hash,
        phase_b_completion_hash=completed.completion_hash,
        parameter_space_hash=accepted_set.policy.parameter_space_hash,
        experiment_parameter_policy_hash=accepted_set.policy.experiment_parameter_policy_hash,
        ordered_config_hashes=selected,
        inherited_candidate_index=inherited,
        seed_config_hash=completed.seed_config_hash,
    )
    phase_c_set = _phase_c_candidate_set(
        accepted_set, configs, candidate_set_hash=candidate_set_hash
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
        candidate_set=phase_c_set,
        ordered_members=ordered_members,
    )
    if plan.partition != "train":
        raise PhaseCError("the Phase-C plan is not a TRAIN plan")
    if plan.logical_job_count != PHASE_C_LOGICAL_JOB_BUDGET:
        raise PhaseCError(
            f"Phase-C plan has {plan.logical_job_count} logical jobs, "
            f"expected the {PHASE_C_LOGICAL_JOB_BUDGET} budget ceiling"
        )
    if plan.plan_hash in {phase_a.plan_hash, completed.plan_hash}:
        raise PhaseCError("the Phase-C plan collides with an earlier phase's plan")

    return PhaseCAuthority(
        baseline_protocol_hash=phase_a.baseline_protocol_hash,
        source_phase_b_plan_hash=completed.plan_hash,
        phase_b_completion_hash=completed.completion_hash,
        phase_c_candidate_set_hash=candidate_set_hash,
        execution_environment_hash=completed.execution_environment_hash,
        seed_config_hash=completed.seed_config_hash,
        train_schedule_manifest_sha256=phase_b.train_schedule_manifest_sha256,
        split_manifest_sha256=split_manifest_sha256(),
        plan=plan,
        configs=configs,
        ordered_config_hashes=selected,
        inherited_candidate_index=inherited,
        member_chromosomes=tuple(m.chromosome for m in scheduled),
    )
