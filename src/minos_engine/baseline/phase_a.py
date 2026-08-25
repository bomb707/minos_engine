"""The deterministic Phase-A execution authority and its canary identity.

Phase A is the frozen protocol's sensitivity screen: the accepted 39 one-at-a-time candidates
over the first chromosome-balanced TRAIN batch — five members, one per chromosome — giving
exactly 195 logical jobs.

Two properties make this an *authority* rather than a convenience:

* **Nothing is caller-selected.** Members come from the committed TRAIN schedule, scientific
  member identities come verbatim from the accepted 50-member plan, and configs come from the
  immutable accepted candidate set. There is no parameter by which a caller could substitute a
  member, a config, a hash or a schedule.
* **The canary is chosen before any score exists.** It is defined structurally as logical job
  ``0`` — member 0 (chr18) against config 0 (the accepted seed) — so it cannot be picked after
  looking at results. Because it is a genuine Phase-A job rather than an extra run, its exact
  immutable execution may later be reused inside Phase A, which can only *reduce* the frozen
  budget.

The historical ``plan_hash`` and ``job_key`` formulas are reused unchanged; no second identity
formula is introduced, and the accepted 50-member plan is never mutated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.baseline.schedule import CHROMOSOMES, build_train_schedule
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.experiments.plan import (
    ExperimentPlan,
    ExperimentPlanMember,
    _assemble_experiment_plan,
    iter_logical_jobs,
)

__all__ = [
    "PHASE_A_AUTHORITY_DOMAIN",
    "PHASE_A_AUTHORITY_MANIFEST",
    "PHASE_A_CANDIDATE_COUNT",
    "PHASE_A_LOGICAL_JOB_COUNT",
    "PHASE_A_MEMBER_COUNT",
    "PHASE_A_PHASE",
    "CanaryIdentity",
    "PhaseAAuthority",
    "PhaseAError",
    "build_phase_a_authority",
    "build_phase_a_plan",
    "load_committed_phase_a_authority",
]

PHASE_A_PHASE = "PHASE_A"
PHASE_A_MEMBER_COUNT = len(CHROMOSOMES)  # 5 — batch 0, one member per chromosome
PHASE_A_CANDIDATE_COUNT = 39
PHASE_A_LOGICAL_JOB_COUNT = PHASE_A_MEMBER_COUNT * PHASE_A_CANDIDATE_COUNT  # 195

PHASE_A_AUTHORITY_DOMAIN = "minos:l2f2-phase-a-execution-authority:v1\n"
PHASE_A_AUTHORITY_MANIFEST = "manifests/l2f2_phase_a_execution_authority_v1.json"

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class PhaseAError(MinosEngineError):
    """The Phase-A authority cannot be derived from the committed sources."""


class CanaryIdentity(BaseModel):
    """Logical job 0 of the Phase-A plan — the ONLY job the canary may execute."""

    model_config = _STRICT

    logical_index: int = Field(ge=0)
    job_key: str = Field(min_length=64, max_length=64)
    member_index: int = Field(ge=0)
    dataset_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)
    config_index: int = Field(ge=0)
    config_hash: str = Field(min_length=64, max_length=64)


class PhaseAAuthority(BaseModel):
    """The complete Phase-A execution authority, cross-bound to the frozen L2-F2-B protocol."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    baseline_protocol_hash: str = Field(min_length=64, max_length=64)
    train_schedule_manifest_sha256: str = Field(min_length=64, max_length=64)
    split_manifest_sha256: str = Field(min_length=64, max_length=64)
    plan: ExperimentPlan
    canary: CanaryIdentity

    @property
    def plan_hash(self) -> str:
        return self.plan.plan_hash

    def content(self) -> dict[str, Any]:
        """Canonical, score-free authority content. No truth, no path, no timestamp, no UUID."""
        return {
            "schema_version": "l2f2-phase-a-execution-authority-v1",
            "phase": PHASE_A_PHASE,
            "baseline_protocol_hash": self.baseline_protocol_hash,
            "train_schedule_manifest_sha256": self.train_schedule_manifest_sha256,
            "split_manifest_sha256": self.split_manifest_sha256,
            "plan": {
                "plan_hash": self.plan.plan_hash,
                "schema_version": self.plan.schema_version,
                "epoch": self.plan.epoch,
                "partition": self.plan.partition,
                "snapshot_hash": self.plan.snapshot_hash,
                "split_manifest_hash": self.plan.split_manifest_hash,
                "registry_snapshot_hash": self.plan.registry_snapshot_hash,
                "train_matrix_hash": self.plan.train_matrix_hash,
                "train_feature_view_hash": self.plan.train_feature_view_hash,
                "feature_set_hash": self.plan.feature_set_hash,
                "feature_registry_hash": self.plan.feature_registry_hash,
                "gatk_registry_hash": self.plan.gatk_registry_hash,
                "parameter_space_hash": self.plan.parameter_space_hash,
                "experiment_parameter_policy_hash": self.plan.experiment_parameter_policy_hash,
                "candidate_set_hash": self.plan.candidate_set_hash,
                "members": [
                    {
                        "member_index": m.member_index,
                        "dataset_id": m.dataset_id,
                        "profile_id": m.profile_id,
                        "content_hash": m.content_hash,
                        "feature_values_hash": m.feature_values_hash,
                        "vector_hash": m.vector_hash,
                    }
                    for m in self.plan.members
                ],
                "config_hashes": [c.config_hash for c in self.plan.configs],
                "train_member_count": self.plan.train_member_count,
                "candidate_count": self.plan.candidate_count,
                "logical_job_count": self.plan.logical_job_count,
            },
            "canary": self.canary.model_dump(mode="json"),
        }

    @property
    def authority_hash(self) -> str:
        return sha256_hex(
            PHASE_A_AUTHORITY_DOMAIN.encode("utf-8") + canonical_json_bytes(self.content())
        )


def _repository_root() -> Path:
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    return repository_root()


def build_phase_a_plan(root: Path | None = None) -> ExperimentPlan:
    """The frozen Phase-A plan: batch 0 of the TRAIN schedule × the accepted 39 candidates.

    Member scientific identities are taken VERBATIM from the accepted 50-member plan; only the
    Phase-A local ``member_index`` is renumbered 0..4. The accepted plan itself is never mutated.
    """
    accepted = build_accepted_experiment_plan()
    schedule = build_train_schedule(root)
    batch = schedule.batches[0]
    if len(batch) != PHASE_A_MEMBER_COUNT:
        raise PhaseAError(
            f"Phase-A batch 0 has {len(batch)} members, expected {PHASE_A_MEMBER_COUNT}"
        )

    by_dataset = {m.dataset_id: m for m in accepted.members}
    ordered: list[ExperimentPlanMember] = []
    for index, member in enumerate(batch):
        source = by_dataset.get(member.dataset_id)
        if source is None:
            raise PhaseAError(
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

    candidate_set = generate_accepted_candidate_set()
    if candidate_set.candidate_count != PHASE_A_CANDIDATE_COUNT:
        raise PhaseAError(
            f"accepted candidate set has {candidate_set.candidate_count} configs, "
            f"expected {PHASE_A_CANDIDATE_COUNT}"
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
        candidate_set=candidate_set,
        ordered_members=ordered,
    )
    if plan.partition != "train":
        raise PhaseAError("the Phase-A plan is not a TRAIN plan")
    if plan.logical_job_count != PHASE_A_LOGICAL_JOB_COUNT:
        raise PhaseAError(
            f"Phase-A plan has {plan.logical_job_count} logical jobs, "
            f"expected {PHASE_A_LOGICAL_JOB_COUNT}"
        )
    return plan


def build_phase_a_authority(root: Path | None = None) -> PhaseAAuthority:
    """The Phase-A plan plus its cross-bound canary identity. Fully deterministic."""
    from minos_engine.baseline.protocol import build_baseline_protocol
    from minos_engine.baseline.schedule import split_manifest_sha256

    base = root or _repository_root()
    plan = build_phase_a_plan(base)
    schedule = build_train_schedule(base)

    first = next(iter_logical_jobs(plan))
    if (first.member_index, first.config_index) != (0, 0):
        raise PhaseAError(
            "logical job 0 is not member 0 / config 0; the canary rule assumes member-major "
            "ordering"
        )
    member = plan.members[first.member_index]
    scheduled = schedule.batches[0][first.member_index]
    if scheduled.dataset_id != member.dataset_id:
        raise PhaseAError("the canary member does not match TRAIN schedule batch 0 position 0")

    canary = CanaryIdentity(
        logical_index=0,
        job_key=first.job_key,
        member_index=first.member_index,
        dataset_id=member.dataset_id,
        round_id=scheduled.round_id,
        chromosome=scheduled.chromosome,
        config_index=first.config_index,
        config_hash=plan.configs[first.config_index].config_hash,
    )
    if canary.config_hash != generate_accepted_candidate_set().seed_config_hash:
        raise PhaseAError("Phase-A config 0 is not the accepted seed config")

    schedule_manifest = base / "manifests" / "l2f2_train_schedule_v1.json"
    if not schedule_manifest.is_file():
        raise PhaseAError(f"committed TRAIN schedule manifest is missing: {schedule_manifest}")
    import hashlib

    return PhaseAAuthority(
        baseline_protocol_hash=build_baseline_protocol(base).protocol_hash,
        train_schedule_manifest_sha256=hashlib.sha256(schedule_manifest.read_bytes()).hexdigest(),
        split_manifest_sha256=split_manifest_sha256(base),
        plan=plan,
        canary=canary,
    )


def load_committed_phase_a_authority(root: Path | None = None) -> dict[str, Any]:
    """Read the committed authority manifest and verify it against the code. Fails closed."""
    base = root or _repository_root()
    path = base / PHASE_A_AUTHORITY_MANIFEST
    if not path.is_file():
        raise PhaseAError(f"committed Phase-A authority manifest is missing: {path}")
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PhaseAError(f"Phase-A authority manifest is not valid JSON: {exc}") from exc

    authority = build_phase_a_authority(base)
    if document.get("authority_hash") != authority.authority_hash:
        raise PhaseAError(
            "committed Phase-A authority hash does not match the authority the code derives"
        )
    if document.get("content") != authority.content():
        raise PhaseAError(
            "committed Phase-A authority content does not match the authority the code derives"
        )
    return document
