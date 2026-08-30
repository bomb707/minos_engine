"""The durable Phase-D plan contract: what a VALIDATION campaign looks like on disk.

``ExperimentPlan`` is a TRAIN plan — its partition is a ``Literal["train"]`` and its hash writes
that constant — and it stays that way. Widening it so validation could reuse it would mean every
TRAIN invariant became conditional, which is exactly the trade this stage must not make. So Phase D
gets its own contract, and the two never share a type.

What a durable validation member binds, and why each part is here:

* ``dataset_id``, ``round_id``, ``chromosome`` — which round was run;
* ``dataset_registry_id``, ``profile_snapshot_member_id``, ``bam_profile_id`` — the authoritative
  upstream rows, so the member is a pointer into accepted lineage rather than a copy of it;
* ``profile_id``, ``content_hash``, ``feature_values_hash`` — the BAM profile's identity. These
  exist for a VALIDATION member exactly as they do for a TRAIN one: they come from
  ``profiling.profile_snapshot_members``, which has always admitted all three partitions.

What it deliberately does NOT bind is any feature-matrix identity. The matrix is how Phase A and
Phase B *chose* candidates from BAM features; Phase D chooses nothing, so there is no matrix and
the honest record of that is its absence. ``0022``'s partition-conditional constraint requires
those columns to be NULL for validation, so this contract cannot drift into inventing them.

The plan identity is not recomputed here. It IS ``PhaseDAuthority.plan_hash`` — the value that
already commits to the finalist-freeze digest, the Phase-C closure digest, the ordered four, the
inherited indices, the ten members, the seed and the no-racing rule. A second formula would be a
second opinion about what this campaign is.
"""

from __future__ import annotations

from dataclasses import dataclass

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "VALIDATION_PLAN_PARTITION",
    "ValidationPlan",
    "ValidationPlanConfig",
    "ValidationPlanError",
    "ValidationPlanMember",
]

#: the one partition this contract admits. Not a parameter — a constant, like TRAIN's.
VALIDATION_PLAN_PARTITION = "validation"


class ValidationPlanError(MinosEngineError):
    """A durable validation plan does not describe the frozen Phase-D campaign."""


@dataclass(frozen=True, slots=True)
class ValidationPlanMember:
    """One durable VALIDATION member: a pointer into accepted upstream lineage."""

    member_index: int
    dataset_id: str
    round_id: str
    chromosome: str
    dataset_registry_id: str
    profile_snapshot_member_id: str
    bam_profile_id: str
    profile_id: str
    content_hash: str
    feature_values_hash: str

    #: the partition is stated, not passed. A member of this type is a validation member.
    @property
    def partition(self) -> str:
        return VALIDATION_PLAN_PARTITION


@dataclass(frozen=True, slots=True)
class ValidationPlanConfig:
    """One durable frozen finalist, with the payload identity execution will resolve."""

    config_index: int
    config_hash: str
    parameter_space_hash: str
    payload_sha256: str
    payload_media_type: str
    payload_uri: str
    payload_size_bytes: int
    inherited_candidate_index: int


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    """The complete durable Phase-D plan. Identity is the authority's, not a second formula."""

    plan_hash: str
    profile_snapshot_id: str
    snapshot_hash: str
    split_manifest_hash: str
    registry_snapshot_hash: str
    gatk_registry_hash: str
    parameter_space_hash: str
    experiment_parameter_policy_hash: str
    candidate_set_hash: str
    members: tuple[ValidationPlanMember, ...]
    configs: tuple[ValidationPlanConfig, ...]

    def __post_init__(self) -> None:
        if len(self.members) != 10:
            raise ValidationPlanError(
                f"a validation plan spans exactly ten VALIDATION members, got {len(self.members)}"
            )
        if len(self.configs) != 4:
            raise ValidationPlanError(
                f"a validation plan confirms exactly four finalists, got {len(self.configs)}"
            )
        if [m.member_index for m in self.members] != list(range(10)):
            raise ValidationPlanError("validation member indices must be exactly 0..9, in order")
        if [c.config_index for c in self.configs] != list(range(4)):
            raise ValidationPlanError("validation config indices must be exactly 0..3, in order")
        if len({m.dataset_id for m in self.members}) != 10:
            raise ValidationPlanError("a validation plan repeats a dataset")
        if len({c.config_hash for c in self.configs}) != 4:
            raise ValidationPlanError("a validation plan repeats a configuration")

    @property
    def partition(self) -> str:
        return VALIDATION_PLAN_PARTITION

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def candidate_count(self) -> int:
        return len(self.configs)

    @property
    def logical_job_count(self) -> int:
        return self.member_count * self.candidate_count

    @property
    def ordered_config_hashes(self) -> tuple[str, ...]:
        return tuple(c.config_hash for c in self.configs)

    @property
    def inherited_candidate_indices(self) -> tuple[int, ...]:
        return tuple(c.inherited_candidate_index for c in self.configs)
