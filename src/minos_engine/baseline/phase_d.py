"""THE L2-F2-F validation authority: four frozen finalists across all ten VALIDATION members.

Phase D is not a smaller Phase C. Phase C searched — it raced, it eliminated, and which candidate
received which member depended on what earlier batches had already shown. Phase D confirms, and
confirmation has no branches: the four configurations were frozen before any validation byte
existed, and each of them receives every one of the ten VALIDATION members. Forty evaluations,
decided in advance.

That difference is enforced structurally rather than by convention:

* there is no racing function in this module and none is imported. The frozen protocol records the
  rule as ``NONE_EVERY_FINALIST_RECEIVES_EVERY_MEMBER``, and a module that cannot eliminate cannot
  eliminate by accident;
* the finalist set arrives through :mod:`minos_engine.baseline.finalist_freeze`, verified against
  the artifact digest and every scientific identity, so a validation run cannot quietly choose its
  own four;
* the members arrive through :mod:`minos_engine.baseline.validation_members`, which selects the
  VALIDATION partition out of the frozen split manifest and refuses TRAIN and TEST;
* the plan identity binds all of that. No runtime outcome is an input — the same frozen inputs
  produce the same plan hash before a single job has run, and after all forty have.

A badly performing finalist is still evaluated on all ten members. The campaign stops for OUR
failures, never for a candidate's.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from minos_engine.baseline.finalist_freeze import FinalistFreeze
from minos_engine.baseline.protocol import PHASE_D_MEMBER_COUNT
from minos_engine.baseline.racing import VALIDATION_FINALIST_COUNT
from minos_engine.baseline.validation_members import (
    ValidationSchedule,
    build_validation_schedule,
)
from minos_engine.common.errors import MinosEngineError

__all__ = [
    "PHASE_D_CANDIDATE_COUNT",
    "PHASE_D_LOGICAL_JOB_BUDGET",
    "PHASE_D_PHASE",
    "PHASE_D_PLAN_DOMAIN",
    "PHASE_D_PLAN_SCHEMA",
    "PHASE_D_RACING_RULE",
    "PhaseDAuthority",
    "PhaseDError",
    "ValidationPair",
    "build_l2f2_phase_d_authority",
    "compute_phase_d_plan_hash",
]

#: the database's phase vocabulary continues PHASE_A/PHASE_B/PHASE_C. The protocol's budget key is
#: ``phase_d`` and its member constant ``PHASE_D_MEMBER_COUNT``; one name, used everywhere.
PHASE_D_PHASE = "PHASE_D"
PHASE_D_CANDIDATE_COUNT = VALIDATION_FINALIST_COUNT  # 4
PHASE_D_LOGICAL_JOB_BUDGET = PHASE_D_CANDIDATE_COUNT * PHASE_D_MEMBER_COUNT  # 40

#: recorded in the plan identity so the absence of racing is part of what the hash commits to.
PHASE_D_RACING_RULE = "NONE_EVERY_FINALIST_RECEIVES_EVERY_MEMBER"

PHASE_D_PLAN_SCHEMA = "l2f2-phase-d-validation-plan-v1"
PHASE_D_PLAN_DOMAIN = "minos:l2f2-phase-d-validation-plan:v1\n"


class PhaseDError(MinosEngineError):
    """The L2-F2-F validation authority cannot be derived as the frozen protocol requires."""


@dataclass(frozen=True, slots=True)
class ValidationPair:
    """One logical validation job: one frozen finalist on one VALIDATION member."""

    config_hash: str
    inherited_candidate_index: int
    dataset_id: str
    chromosome: str
    member_index: int
    finalist_index: int

    @property
    def logical_key(self) -> str:
        """A stable identity for this pair. Distinct configurations never collide."""
        payload = f"{self.config_hash}\n{self.dataset_id}\n{self.chromosome}\n"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PhaseDAuthority:
    """Everything L2-F2-F may act on, and nothing that could let it choose differently."""

    baseline_protocol_hash: str
    finalist_freeze_sha256: str
    phase_c_closure_sha256: str
    phase_c_plan_hash: str
    phase_c_candidate_set_hash: str
    phase_b_completion_hash: str
    parameter_space_hash: str
    execution_environment_hash: str
    scoring_contract_hash: str
    minos_subnet_sha: str
    split_manifest_sha256: str
    ordered_config_hashes: tuple[str, ...]
    seed_config_hash: str
    inherited_candidate_index: dict[str, int]
    schedule: ValidationSchedule
    plan_hash: str

    @property
    def member_count(self) -> int:
        return len(self.schedule.members)

    @property
    def candidate_count(self) -> int:
        return len(self.ordered_config_hashes)

    @property
    def logical_job_count(self) -> int:
        return self.candidate_count * self.member_count

    def required_pairs(self) -> tuple[tuple[str, str], ...]:
        """``(dataset_id, chromosome)`` for all ten VALIDATION members, in plan order."""
        return self.schedule.required_pairs()

    def pairs(self) -> tuple[ValidationPair, ...]:
        """All forty logical jobs: every finalist on every member, finalist-major, member-minor.

        The order is deterministic and derived from the frozen finalist order and the manifest's
        committed member order, so it is the same on every machine and in every process.
        """
        out: list[ValidationPair] = []
        for finalist_index, config_hash in enumerate(self.ordered_config_hashes):
            for member in self.schedule.members:
                out.append(
                    ValidationPair(
                        config_hash=config_hash,
                        inherited_candidate_index=self.inherited_candidate_index[config_hash],
                        dataset_id=member.dataset_id,
                        chromosome=member.chromosome,
                        member_index=member.member_index,
                        finalist_index=finalist_index,
                    )
                )
        return tuple(out)


def compute_phase_d_plan_hash(
    *,
    baseline_protocol_hash: str,
    finalist_freeze_sha256: str,
    phase_c_closure_sha256: str,
    phase_c_plan_hash: str,
    phase_c_candidate_set_hash: str,
    phase_b_completion_hash: str,
    parameter_space_hash: str,
    execution_environment_hash: str,
    scoring_contract_hash: str,
    minos_subnet_sha: str,
    split_manifest_sha256: str,
    ordered_config_hashes: tuple[str, ...],
    seed_config_hash: str,
    inherited_candidate_index: dict[str, int],
    member_pairs: tuple[tuple[str, str], ...],
) -> str:
    """The L2-F2-F plan identity.

    Every input is frozen before validation begins. There is deliberately no observation, no score,
    no runtime measurement and no job state here: the plan hash must be computable — and identical
    — both before the first validation job exists and after the fortieth has been decided.
    """
    if len(ordered_config_hashes) != PHASE_D_CANDIDATE_COUNT:
        raise PhaseDError(
            f"a validation plan confirms exactly {PHASE_D_CANDIDATE_COUNT} finalists, "
            f"got {len(ordered_config_hashes)}"
        )
    if len(member_pairs) != PHASE_D_MEMBER_COUNT:
        raise PhaseDError(
            f"a validation plan spans exactly {PHASE_D_MEMBER_COUNT} VALIDATION members, "
            f"got {len(member_pairs)}"
        )
    payload = {
        "schema_version": PHASE_D_PLAN_SCHEMA,
        "stage": "L2-F2-F",
        "phase": PHASE_D_PHASE,
        "racing": PHASE_D_RACING_RULE,
        "baseline_protocol_hash": baseline_protocol_hash,
        "finalist_freeze_sha256": finalist_freeze_sha256,
        "phase_c_closure_sha256": phase_c_closure_sha256,
        "phase_c_plan_hash": phase_c_plan_hash,
        "phase_c_candidate_set_hash": phase_c_candidate_set_hash,
        "phase_b_completion_hash": phase_b_completion_hash,
        "parameter_space_hash": parameter_space_hash,
        "execution_environment_hash": execution_environment_hash,
        "scoring_contract_hash": scoring_contract_hash,
        "minos_subnet_sha": minos_subnet_sha,
        "split_manifest_sha256": split_manifest_sha256,
        "ordered_config_hashes": list(ordered_config_hashes),
        "seed_config_hash": seed_config_hash,
        "inherited_candidate_index": [
            [h, inherited_candidate_index[h]] for h in ordered_config_hashes
        ],
        "validation_members": [list(pair) for pair in member_pairs],
        "candidate_count": PHASE_D_CANDIDATE_COUNT,
        "member_count": PHASE_D_MEMBER_COUNT,
        "logical_job_count": PHASE_D_LOGICAL_JOB_BUDGET,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(PHASE_D_PLAN_DOMAIN.encode("utf-8") + encoded.encode("utf-8")).hexdigest()


def build_l2f2_phase_d_authority(
    freeze: FinalistFreeze,
    *,
    schedule: ValidationSchedule | None = None,
) -> PhaseDAuthority:
    """Derive the validation authority from a VERIFIED freeze and the frozen VALIDATION schedule.

    The freeze must already have been verified by ``load_finalist_freeze``; this function does not
    accept a raw document, so there is no path from an unverified file to a validation plan.
    """
    resolved = schedule or build_validation_schedule()
    if len(resolved.members) != PHASE_D_MEMBER_COUNT:
        raise PhaseDError(
            f"the VALIDATION schedule holds {len(resolved.members)} members, the protocol fixes "
            f"{PHASE_D_MEMBER_COUNT}"
        )
    if len(freeze.ordered_finalists) != PHASE_D_CANDIDATE_COUNT:
        raise PhaseDError(
            f"the frozen outcome names {len(freeze.ordered_finalists)} finalists, the protocol "
            f"fixes {PHASE_D_CANDIDATE_COUNT}"
        )
    if freeze.seed_config_hash not in freeze.ordered_finalists:  # pragma: no cover - verified
        raise PhaseDError("the frozen outcome does not include the seed")

    plan_hash = compute_phase_d_plan_hash(
        baseline_protocol_hash=freeze.baseline_protocol_hash,
        finalist_freeze_sha256=freeze.artifact_sha256,
        phase_c_closure_sha256=freeze.phase_c_closure_sha256,
        phase_c_plan_hash=freeze.phase_c_plan_hash,
        phase_c_candidate_set_hash=freeze.phase_c_candidate_set_hash,
        phase_b_completion_hash=freeze.phase_b_completion_hash,
        parameter_space_hash=freeze.parameter_space_hash,
        execution_environment_hash=freeze.execution_environment_hash,
        scoring_contract_hash=freeze.scoring_contract_hash,
        minos_subnet_sha=freeze.minos_subnet_sha,
        split_manifest_sha256=resolved.split_manifest_sha256,
        ordered_config_hashes=freeze.ordered_finalists,
        seed_config_hash=freeze.seed_config_hash,
        inherited_candidate_index=freeze.inherited_candidate_index,
        member_pairs=resolved.required_pairs(),
    )
    authority = PhaseDAuthority(
        baseline_protocol_hash=freeze.baseline_protocol_hash,
        finalist_freeze_sha256=freeze.artifact_sha256,
        phase_c_closure_sha256=freeze.phase_c_closure_sha256,
        phase_c_plan_hash=freeze.phase_c_plan_hash,
        phase_c_candidate_set_hash=freeze.phase_c_candidate_set_hash,
        phase_b_completion_hash=freeze.phase_b_completion_hash,
        parameter_space_hash=freeze.parameter_space_hash,
        execution_environment_hash=freeze.execution_environment_hash,
        scoring_contract_hash=freeze.scoring_contract_hash,
        minos_subnet_sha=freeze.minos_subnet_sha,
        split_manifest_sha256=resolved.split_manifest_sha256,
        ordered_config_hashes=freeze.ordered_finalists,
        seed_config_hash=freeze.seed_config_hash,
        inherited_candidate_index=dict(freeze.inherited_candidate_index),
        schedule=resolved,
        plan_hash=plan_hash,
    )
    pairs = authority.pairs()
    if len(pairs) != PHASE_D_LOGICAL_JOB_BUDGET:  # pragma: no cover - arithmetic above
        raise PhaseDError(
            f"the validation plan produced {len(pairs)} logical jobs, expected "
            f"{PHASE_D_LOGICAL_JOB_BUDGET}"
        )
    if len({p.logical_key for p in pairs}) != PHASE_D_LOGICAL_JOB_BUDGET:
        raise PhaseDError("the validation plan repeats a (config, member) pair")
    covered = {(p.config_hash, p.dataset_id) for p in pairs}
    expected = {
        (config_hash, member.dataset_id)
        for config_hash in freeze.ordered_finalists
        for member in resolved.members
    }
    if covered != expected:
        raise PhaseDError(
            "the validation plan is not the complete cross product; every finalist must receive "
            "every VALIDATION member exactly once"
        )
    return authority
