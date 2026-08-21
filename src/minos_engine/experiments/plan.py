"""L2-F F3-B pure ``ExperimentPlan`` contracts + frozen hash formulas (no I/O).

This module is a **pure** contract layer. It performs no PostgreSQL writes, artifact
publication, job enqueueing, claiming, execution, scoring, training, optimization, or
configuration selection. It defines immutable, strictly-validated plan/member/config
contracts, the two domain-separated hash formulas (``plan_hash`` and ``job_key``), and a pure
in-memory logical-job enumerator.

Counts are **derived** from the consumed inventory (train membership × accepted candidate
set); no percentage or magic count is encoded as a contract constant. The epoch-1 figures
(50 train members, 41 candidates, 2050 logical jobs) are historical derived results, not
inputs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex

if TYPE_CHECKING:
    from minos_engine.experiments.candidates import CandidateSet

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "PLAN_PARTITION",
    "PLAN_HASH_DOMAIN",
    "JOB_KEY_DOMAIN",
    "ExperimentPlanMember",
    "ExperimentPlanConfig",
    "ExperimentPlan",
    "compute_plan_hash",
    "compute_job_key",
    "LogicalJob",
    "iter_logical_jobs",
    "logical_job_keys",
]

PLAN_SCHEMA_VERSION = "l2f-experiment-plan-v1"
PLAN_PARTITION = "train"
#: domain-separation prefixes prepended (as bytes) before the canonical-JSON preimage.
PLAN_HASH_DOMAIN = "minos:l2f-experiment-plan:v1\n"
JOB_KEY_DOMAIN = "minos:l2f-job:v1\n"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _validate_hex64(v: str) -> str:
    if not _HEX64.fullmatch(v):
        raise ValueError("must be a lowercase 64-character hex string")
    return v


Hex64 = Annotated[str, AfterValidator(_validate_hex64)]
_STRICT = ConfigDict(frozen=True, extra="forbid", strict=True)


class ExperimentPlanMember(BaseModel):
    """One immutable train-matrix member of a plan (verbatim from accepted membership)."""

    model_config = _STRICT

    dataset_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    content_hash: Hex64
    feature_values_hash: Hex64
    vector_hash: Hex64
    member_index: int = Field(ge=0)


class ExperimentPlanConfig(BaseModel):
    """One immutable candidate config of a plan (verbatim from the accepted candidate set)."""

    model_config = _STRICT

    config_index: int = Field(ge=0)
    config_hash: Hex64
    parameter_space_hash: Hex64


def compute_plan_hash(
    *,
    schema_version: str,
    epoch: int,
    snapshot_hash: str,
    split_manifest_hash: str,
    registry_snapshot_hash: str,
    train_matrix_hash: str,
    train_feature_view_hash: str,
    feature_set_hash: str,
    feature_registry_hash: str,
    gatk_registry_hash: str,
    parameter_space_hash: str,
    experiment_parameter_policy_hash: str,
    candidate_set_hash: str,
    members: Sequence[ExperimentPlanMember],
    configs: Sequence[ExperimentPlanConfig],
    train_member_count: int,
    candidate_count: int,
    logical_job_count: int,
) -> str:
    """Domain-separated plan identity.

    ``sha256(PLAN_HASH_DOMAIN + canonical_json(content))`` where ``content`` is the complete
    plan identity with ``partition`` fixed to ``train`` and the ``plan_hash`` field itself
    excluded from the preimage. No timestamps/UUIDs/paths/URLs/hostnames/worker identities/
    claim state/scores/labels/truth/raw features/artifact locations enter the preimage.
    """
    content: dict[str, Any] = {
        "schema_version": schema_version,
        "epoch": epoch,
        "partition": PLAN_PARTITION,
        "snapshot_hash": snapshot_hash,
        "split_manifest_hash": split_manifest_hash,
        "registry_snapshot_hash": registry_snapshot_hash,
        "train_matrix_hash": train_matrix_hash,
        "train_feature_view_hash": train_feature_view_hash,
        "feature_set_hash": feature_set_hash,
        "feature_registry_hash": feature_registry_hash,
        "gatk_registry_hash": gatk_registry_hash,
        "parameter_space_hash": parameter_space_hash,
        "experiment_parameter_policy_hash": experiment_parameter_policy_hash,
        "candidate_set_hash": candidate_set_hash,
        "ordered_members": [
            {
                "dataset_id": m.dataset_id,
                "profile_id": m.profile_id,
                "content_hash": m.content_hash,
                "feature_values_hash": m.feature_values_hash,
                "vector_hash": m.vector_hash,
                "member_index": m.member_index,
            }
            for m in members
        ],
        "ordered_configs": [
            {
                "config_index": c.config_index,
                "config_hash": c.config_hash,
                "parameter_space_hash": c.parameter_space_hash,
            }
            for c in configs
        ],
        "train_member_count": train_member_count,
        "candidate_count": candidate_count,
        "logical_job_count": logical_job_count,
    }
    return sha256_hex(PLAN_HASH_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def compute_job_key(
    *,
    plan_hash: str,
    member_index: int,
    dataset_id: str,
    profile_id: str,
    content_hash: str,
    feature_values_hash: str,
    config_index: int,
    config_hash: str,
) -> str:
    """Domain-separated logical job identity (no execution/claim/score/nondeterministic data)."""
    content = {
        "plan_hash": plan_hash,
        "member_index": member_index,
        "dataset_id": dataset_id,
        "profile_id": profile_id,
        "content_hash": content_hash,
        "feature_values_hash": feature_values_hash,
        "config_index": config_index,
        "config_hash": config_hash,
    }
    return sha256_hex(JOB_KEY_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


class ExperimentPlan(BaseModel):
    """The complete, immutable, self-binding L2-F experiment plan (partition fixed to train)."""

    model_config = _STRICT

    schema_version: Literal["l2f-experiment-plan-v1"]
    epoch: int = Field(ge=1)
    partition: Literal["train"]
    snapshot_hash: Hex64
    split_manifest_hash: Hex64
    registry_snapshot_hash: Hex64
    train_matrix_hash: Hex64
    train_feature_view_hash: Hex64
    feature_set_hash: Hex64
    feature_registry_hash: Hex64
    gatk_registry_hash: Hex64
    parameter_space_hash: Hex64
    experiment_parameter_policy_hash: Hex64
    candidate_set_hash: Hex64
    members: tuple[ExperimentPlanMember, ...] = Field(min_length=1)
    configs: tuple[ExperimentPlanConfig, ...] = Field(min_length=1)
    train_member_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    logical_job_count: int = Field(ge=0)
    plan_hash: Hex64

    @model_validator(mode="after")
    def _validate(self) -> ExperimentPlan:
        # members: contiguous, ordered indices; unique dataset ids and vector hashes.
        for i, m in enumerate(self.members):
            if m.member_index != i:
                raise ValueError(
                    f"member_index must be contiguous in order: position {i} has "
                    f"member_index {m.member_index}"
                )
        if len({m.dataset_id for m in self.members}) != len(self.members):
            raise ValueError("duplicate member dataset_id")
        if len({m.vector_hash for m in self.members}) != len(self.members):
            raise ValueError("duplicate member vector_hash")

        # configs: contiguous, ordered indices; unique config hashes; parameter space bound.
        for i, c in enumerate(self.configs):
            if c.config_index != i:
                raise ValueError(
                    f"config_index must be contiguous in order: position {i} has "
                    f"config_index {c.config_index}"
                )
            if c.parameter_space_hash != self.parameter_space_hash:
                raise ValueError("config parameter_space_hash does not match the plan")
        if len({c.config_hash for c in self.configs}) != len(self.configs):
            raise ValueError("duplicate config_hash")

        # counts derived from inventory; logical job count is the exact product.
        if self.train_member_count != len(self.members):
            raise ValueError("train_member_count does not equal the number of members")
        if self.candidate_count != len(self.configs):
            raise ValueError("candidate_count does not equal the number of configs")
        if self.logical_job_count != self.train_member_count * self.candidate_count:
            raise ValueError("logical_job_count must equal train_member_count * candidate_count")

        # self-binding plan hash.
        expected = compute_plan_hash(
            schema_version=self.schema_version,
            epoch=self.epoch,
            snapshot_hash=self.snapshot_hash,
            split_manifest_hash=self.split_manifest_hash,
            registry_snapshot_hash=self.registry_snapshot_hash,
            train_matrix_hash=self.train_matrix_hash,
            train_feature_view_hash=self.train_feature_view_hash,
            feature_set_hash=self.feature_set_hash,
            feature_registry_hash=self.feature_registry_hash,
            gatk_registry_hash=self.gatk_registry_hash,
            parameter_space_hash=self.parameter_space_hash,
            experiment_parameter_policy_hash=self.experiment_parameter_policy_hash,
            candidate_set_hash=self.candidate_set_hash,
            members=self.members,
            configs=self.configs,
            train_member_count=self.train_member_count,
            candidate_count=self.candidate_count,
            logical_job_count=self.logical_job_count,
        )
        if self.plan_hash != expected:
            raise ValueError("plan_hash does not bind the plan content")
        return self


@dataclass(frozen=True)
class LogicalJob:
    """One deterministic logical job specification (member × config). Never persisted here."""

    member_index: int
    dataset_id: str
    profile_id: str
    content_hash: str
    feature_values_hash: str
    config_index: int
    config_hash: str
    job_key: str


def iter_logical_jobs(plan: ExperimentPlan) -> Iterator[LogicalJob]:
    """Enumerate the logical product in memory in member-major then config-index order.

    Pure: enumerates only; it never persists, enqueues, claims, or executes. Yields exactly
    ``plan.logical_job_count`` jobs, each with a unique, repeatable ``job_key``.
    """
    for member in plan.members:  # frozen train-matrix member_index order
        for config in plan.configs:  # accepted config_index order
            yield LogicalJob(
                member_index=member.member_index,
                dataset_id=member.dataset_id,
                profile_id=member.profile_id,
                content_hash=member.content_hash,
                feature_values_hash=member.feature_values_hash,
                config_index=config.config_index,
                config_hash=config.config_hash,
                job_key=compute_job_key(
                    plan_hash=plan.plan_hash,
                    member_index=member.member_index,
                    dataset_id=member.dataset_id,
                    profile_id=member.profile_id,
                    content_hash=member.content_hash,
                    feature_values_hash=member.feature_values_hash,
                    config_index=config.config_index,
                    config_hash=config.config_hash,
                ),
            )


def logical_job_keys(plan: ExperimentPlan) -> tuple[str, ...]:
    """The ordered tuple of deterministic job keys (length == ``logical_job_count``)."""
    return tuple(job.job_key for job in iter_logical_jobs(plan))


def _assemble_experiment_plan(
    *,
    epoch: int,
    snapshot_hash: str,
    split_manifest_hash: str,
    registry_snapshot_hash: str,
    train_matrix_hash: str,
    train_feature_view_hash: str,
    feature_set_hash: str,
    feature_registry_hash: str,
    candidate_set: CandidateSet,
    ordered_members: Sequence[ExperimentPlanMember],
) -> ExperimentPlan:
    """PURE structural assembler — NOT a trust boundary and NOT an accepted constructor.

    It verifies nothing and confers no acceptance; it derives the GATK/parameter-space/policy/
    candidate-set/config identities from the supplied ``candidate_set``, derives the counts from
    the supplied inventory, computes the self-binding ``plan_hash`` and returns a validated
    ``ExperimentPlan``. The ONLY accepted/verified boundary is
    ``accepted_plan.build_accepted_experiment_plan`` (no parameters). This helper is private and
    not exported; tests and the accepted constructor call it after establishing their own trust.
    """
    # Each plan-config binds the L2-F scientific parameter-space identity (the policy's
    # live-GATK domain hash), NOT the internal ParameterSpaceSnapshot canonicalization envelope
    # recorded on the CanonicalConfig. The plan validator requires the two to agree.
    configs = tuple(
        ExperimentPlanConfig(
            config_index=i,
            config_hash=c.config_hash,
            parameter_space_hash=candidate_set.policy.parameter_space_hash,
        )
        for i, c in enumerate(candidate_set.configs)
    )
    members = tuple(ordered_members)
    train_member_count = len(members)
    candidate_count = len(configs)
    logical_job_count = train_member_count * candidate_count
    gatk_registry_hash = candidate_set.policy.registry_hash
    parameter_space_hash = candidate_set.policy.parameter_space_hash
    experiment_parameter_policy_hash = candidate_set.policy.experiment_parameter_policy_hash
    candidate_set_hash = candidate_set.candidate_set_hash

    plan_hash = compute_plan_hash(
        schema_version=PLAN_SCHEMA_VERSION,
        epoch=epoch,
        snapshot_hash=snapshot_hash,
        split_manifest_hash=split_manifest_hash,
        registry_snapshot_hash=registry_snapshot_hash,
        train_matrix_hash=train_matrix_hash,
        train_feature_view_hash=train_feature_view_hash,
        feature_set_hash=feature_set_hash,
        feature_registry_hash=feature_registry_hash,
        gatk_registry_hash=gatk_registry_hash,
        parameter_space_hash=parameter_space_hash,
        experiment_parameter_policy_hash=experiment_parameter_policy_hash,
        candidate_set_hash=candidate_set_hash,
        members=members,
        configs=configs,
        train_member_count=train_member_count,
        candidate_count=candidate_count,
        logical_job_count=logical_job_count,
    )
    return ExperimentPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        epoch=epoch,
        partition=PLAN_PARTITION,
        snapshot_hash=snapshot_hash,
        split_manifest_hash=split_manifest_hash,
        registry_snapshot_hash=registry_snapshot_hash,
        train_matrix_hash=train_matrix_hash,
        train_feature_view_hash=train_feature_view_hash,
        feature_set_hash=feature_set_hash,
        feature_registry_hash=feature_registry_hash,
        gatk_registry_hash=gatk_registry_hash,
        parameter_space_hash=parameter_space_hash,
        experiment_parameter_policy_hash=experiment_parameter_policy_hash,
        candidate_set_hash=candidate_set_hash,
        members=members,
        configs=configs,
        train_member_count=train_member_count,
        candidate_count=candidate_count,
        logical_job_count=logical_job_count,
        plan_hash=plan_hash,
    )
