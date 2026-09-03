"""Derive the BASELINE-QUALIFIED checks, and verify the resulting gate offline.

Two halves, deliberately separated. :func:`derive_checks` turns a verified observation into the
42 registered boolean checks and decides nothing else — it computes no score, chooses no winner,
and accepts no caller-supplied outcome. :func:`verify_baseline_qualified_gate` re-checks a
published gate against committed identities alone, with no database, no GATK, no scorer and no
truth: an auditor should be able to disprove this gate on a laptop.

The observation is built from hash-verified inputs. Where a path is operationally unavoidable
(the closure artifact, the qualified source tree) it is verified by digest before anything is read
out of it, so no field arrives on trust.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "BASELINE_QUALIFIED_GATE",
    "BaselineQualificationError",
    "BaselineQualifiedObservation",
    "derive_checks",
    "verify_baseline_qualified_gate",
]

BASELINE_QUALIFIED_GATE: Final = "BASELINE-QUALIFIED"

#: HARNESS-READY prerequisite identities, as recorded in section 13.
HARNESS_READY_GATE_HASH: Final = "0e8411eb"
HARNESS_READY_QUALIFICATION_HASH: Final = "b1d1cc5d"

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class BaselineQualificationError(MinosEngineError):
    """The campaign cannot be qualified as presented."""


class BaselineQualifiedObservation(BaseModel):
    """Everything the gate reasons about, already verified. No outcome is nominable here."""

    model_config = _STRICT

    qualified_source_commit: str = Field(min_length=40, max_length=40)
    qualified_source_tree: str = Field(min_length=40, max_length=40)
    worktree_commit: str = Field(min_length=40, max_length=40)
    worktree_tree: str = Field(min_length=40, max_length=40)
    worktree_clean: bool

    harness_ready_gate_hash_prefix: str = Field(min_length=8)
    harness_ready_qualification_hash_prefix: str = Field(min_length=8)

    closure_artifact_verified: bool
    closure_hash_recomputed: str = Field(min_length=64, max_length=64)
    baseline_selected_hash: str = Field(min_length=64, max_length=64)
    baseline_selected_manifest_verified: bool

    candidate_count: int
    member_count: int
    observation_count: int
    all_candidates_complete: bool
    validation_infrastructure_incidents: int

    selected_config_hash: str = Field(min_length=64, max_length=64)
    selected_rank: int
    selected_inherited_candidate_index: int
    selected_statistics_agree: bool
    seed_config_hash: str = Field(min_length=64, max_length=64)
    seed_rank: int

    scorer_source_identities_exact: bool
    happy_digest: str = Field(min_length=1)
    bcftools_digest: str = Field(min_length=1)

    train: dict[str, bool]

    test_untouched: bool
    train_and_validation_identities_disjoint: bool
    evidence_hashes: dict[str, str]


def derive_checks(observation: BaselineQualifiedObservation) -> dict[str, bool]:
    """Turn a verified observation into the registered check set. Decides nothing on its own."""
    from minos_engine.baseline.baseline_selected import (
        BASELINE_PROTOCOL_HASH,
        EXECUTION_ENVIRONMENT_HASH,
        MINOS_SUBNET_SHA,
        PHASE_D_CLOSURE_HASH,
        SCORING_CONTRACT_HASH,
        SEED_CONFIG_HASH,
        SEED_RANK,
        SELECTED_CONFIG_HASH,
        SELECTED_INHERITED_CANDIDATE_INDEX,
        SELECTION_INTERPRETATION_HASH,
        compute_baseline_selected_hash,
    )
    from minos_engine.baseline.objective import (
        CVAR_ALPHA,
        CVAR_WEIGHT,
        FAILURE_PENALTY,
        FLOOR_WEIGHT,
        MEAN_WEIGHT,
    )
    from minos_engine.baseline.phase_d_selection import (
        compute_selection_interpretation_hash,
    )
    from minos_engine.baseline.protocol import (
        build_baseline_protocol,
        compute_protocol_hash,
        load_committed_protocol,
    )

    checks: dict[str, bool] = {}

    # ---- SOURCE ----------------------------------------------------------------------------
    checks["qualified_source_present"] = bool(observation.qualified_source_commit)
    checks["qualified_source_tree_matches"] = (
        observation.worktree_tree == observation.qualified_source_tree
    )
    checks["worktree_matches_qualified_source"] = (
        observation.worktree_commit == observation.qualified_source_commit
        and observation.worktree_clean
    )

    # ---- PREREQUISITES ---------------------------------------------------------------------
    checks["harness_ready_gate_bound"] = observation.harness_ready_gate_hash_prefix.startswith(
        HARNESS_READY_GATE_HASH
    )
    checks["harness_ready_qualification_bound"] = (
        observation.harness_ready_qualification_hash_prefix.startswith(
            HARNESS_READY_QUALIFICATION_HASH
        )
    )
    checks["phase_d_closure_artifact_bound"] = observation.closure_artifact_verified
    checks["baseline_selected_authority_bound"] = (
        observation.baseline_selected_manifest_verified
        and observation.baseline_selected_hash == compute_baseline_selected_hash()
    )

    # ---- PROTOCOL --------------------------------------------------------------------------
    committed_protocol = load_committed_protocol()
    protocol_hash = str(committed_protocol.get("protocol_hash"))
    checks["baseline_protocol_hash_exact"] = (
        protocol_hash == BASELINE_PROTOCOL_HASH
        and compute_protocol_hash(build_baseline_protocol()) == BASELINE_PROTOCOL_HASH
    )
    checks["objective_authority_exact"] = (
        CVAR_ALPHA,
        CVAR_WEIGHT,
        FLOOR_WEIGHT,
        MEAN_WEIGHT,
        FAILURE_PENALTY,
    ) == (0.25, 0.50, 0.30, 0.20, 1.00)
    design = committed_protocol.get("content", {}).get("decisions", {})
    checks["candidate_design_authority_exact"] = (
        design.get("D8_phase_b_design_family") == "DETERMINISTIC_MIXED_DOMAIN_LATIN_HYPERCUBE"
    )
    checks["selection_interpretation_exact"] = (
        compute_selection_interpretation_hash() == SELECTION_INTERPRETATION_HASH
    )
    # the objective is inside the protocol hash, frozen before the first evaluation existed;
    # an unchanged protocol hash IS the no-post-hoc-change proof.
    checks["no_post_hoc_objective_change"] = (
        checks["baseline_protocol_hash_exact"] and checks["objective_authority_exact"]
    )

    # ---- SCORER ----------------------------------------------------------------------------
    checks["scoring_contract_exact"] = (
        observation.evidence_hashes.get("scoring_contract_hash") == SCORING_CONTRACT_HASH
    )
    checks["minos_subnet_commit_exact"] = (
        observation.evidence_hashes.get("minos_subnet_sha") == MINOS_SUBNET_SHA
    )
    checks["scorer_source_identities_exact"] = observation.scorer_source_identities_exact
    checks["happy_immutable_digest_exact"] = "@sha256:" in observation.happy_digest
    checks["bcftools_immutable_digest_exact"] = "@sha256:" in observation.bcftools_digest

    # ---- TRAIN EVIDENCE --------------------------------------------------------------------
    checks.update(observation.train)

    # ---- VALIDATION EVIDENCE ---------------------------------------------------------------
    checks["validation_closure_hash_recomputed"] = (
        observation.closure_hash_recomputed == PHASE_D_CLOSURE_HASH
    )
    checks["validation_matrix_is_exact_ten_by_four"] = (
        observation.candidate_count == 4 and observation.member_count == 10
    )
    checks["validation_forty_terminal_outcomes"] = observation.observation_count == 40
    checks["validation_all_candidates_complete"] = observation.all_candidates_complete
    checks["validation_no_infrastructure_incident"] = (
        observation.validation_infrastructure_incidents == 0
    )

    # ---- SELECTION -------------------------------------------------------------------------
    checks["selected_config_is_closure_rank_zero"] = (
        observation.selected_config_hash == SELECTED_CONFIG_HASH and observation.selected_rank == 0
    )
    checks["selected_statistics_agree_with_closure"] = observation.selected_statistics_agree
    checks["selected_inherited_index_exact"] = (
        observation.selected_inherited_candidate_index == SELECTED_INHERITED_CANDIDATE_INDEX
    )
    checks["seed_rank_recorded"] = (
        observation.seed_config_hash == SEED_CONFIG_HASH and observation.seed_rank == SEED_RANK
    )
    # the seed did not win, and nothing promoted it: rank alone decided.
    checks["no_seed_override"] = not (
        observation.selected_config_hash == SEED_CONFIG_HASH and observation.seed_rank != 0
    )

    # ---- ISOLATION -------------------------------------------------------------------------
    checks["test_untouched"] = observation.test_untouched
    checks["train_and_validation_identities_not_mixed"] = (
        observation.train_and_validation_identities_disjoint
    )

    # ---- REPRODUCIBILITY -------------------------------------------------------------------
    checks["closure_reproducible_from_committed_identities"] = (
        observation.closure_hash_recomputed == PHASE_D_CLOSURE_HASH
        and observation.closure_artifact_verified
        and observation.evidence_hashes.get("execution_environment_hash")
        == EXECUTION_ENVIRONMENT_HASH
    )
    checks["evidence_hashes_complete"] = all(
        observation.evidence_hashes.get(key)
        for key in (
            "phase_d_activation_evidence",
            "phase_d_execution_evidence",
            "phase_d_sentinel_evidence",
            "phase_d_complete_matrix_evidence",
            "phase_d_closure_artifact",
            "phase_d_closure_evidence",
        )
    )
    return checks


def verify_baseline_qualified_gate(
    *, gate_path: str | Path, qualification_path: str | Path, root: Path | None = None
) -> dict[str, Any]:
    """Verify a published gate offline. No database, no GATK, no scorer, no truth.

    Fails closed on: a missing mandatory check, a mandatory check reported false, an unknown check
    name, a qualified source that disagrees with the qualification, or a baseline-selected
    identity that disagrees with the committed manifest.
    """
    from minos_engine.baseline.baseline_selected import (
        BaselineSelectedError,
        compute_baseline_selected_hash,
        load_committed_baseline_selected,
    )
    from minos_engine.gates.required_checks import required_checks_for

    reasons: list[str] = []
    gate = _read_json(gate_path, label="gate", reasons=reasons)
    qualification = _read_json(qualification_path, label="qualification", reasons=reasons)
    if gate is None or qualification is None:
        return {"gate_name": BASELINE_QUALIFIED_GATE, "ok": False, "reasons": reasons}

    if gate.get("gate_name") != BASELINE_QUALIFIED_GATE:
        reasons.append(f"gate names {gate.get('gate_name')!r}")
    if gate.get("status") != "PASS":
        reasons.append(f"gate status is {gate.get('status')!r}")

    required = required_checks_for(BASELINE_QUALIFIED_GATE)
    observed = dict(qualification.get("checks") or {})
    missing = sorted(required - set(observed))
    if missing:
        reasons.append(f"missing mandatory checks: {missing}")
    unknown = sorted(set(observed) - required)
    if unknown:
        reasons.append(f"unregistered checks present: {unknown}")
    failed = sorted(name for name in required & set(observed) if not observed[name])
    if failed:
        reasons.append(f"mandatory checks reported false: {failed}")

    # the gate must name the QUALIFIED SOURCE, never the evidence commit that carries it.
    for field in ("qualified_source_commit", "qualified_source_tree"):
        if gate.get(field) != qualification.get(field):
            reasons.append(f"gate and qualification disagree on {field}")

    try:
        committed = load_committed_baseline_selected(root)
    except BaselineSelectedError as exc:
        reasons.append(f"committed baseline-selected authority is unusable: {exc}")
    else:
        expected = compute_baseline_selected_hash()
        if committed.get("baseline_selected_hash") != expected:
            reasons.append("committed baseline-selected hash disagrees with source")
        if gate.get("baseline_selected_hash") != expected:
            reasons.append("gate baseline-selected hash disagrees with the committed authority")

    return {
        "gate_name": BASELINE_QUALIFIED_GATE,
        "ok": not reasons,
        "reasons": reasons,
        "required_check_count": len(required),
    }


def _read_json(path: str | Path, *, label: str, reasons: list[str]) -> dict[str, Any] | None:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        reasons.append(f"{label} artifact {target} is missing or a symlink")
        return None
    try:
        document: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        reasons.append(f"{label} artifact is not JSON: {exc}")
        return None
    return document
