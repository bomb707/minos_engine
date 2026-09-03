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
from minos_engine.qualification.l2f2_baseline_qualified_contract import (
    BaselineQualificationResult,
)
from minos_engine.qualification.l2f2_baseline_qualified_qualifier import (
    CLOSURE_AUTHORITY_SOURCE,
    TrustedBaselineQualification,
)

__all__ = [
    "BASELINE_QUALIFIED_GATE",
    "BASELINE_QUALIFIED_GATE_PATH",
    "BASELINE_QUALIFICATION_RESULT_PATH",
    "BaselineQualificationError",
    "BaselineQualifiedObservation",
    "assemble_baseline_qualified_gate",
    "derive_checks",
    "observation_from_result",
    "verify_baseline_qualified_gate",
    "write_baseline_qualification_outputs",
]

BASELINE_QUALIFIED_GATE: Final = "BASELINE-QUALIFIED"

#: FULL prerequisite identities. The eight-character forms once used here were prefixes, not
#: identities: any hash sharing those eight characters would have passed.
from minos_engine.qualification.l2f2_baseline_qualified_contract import (  # noqa: E402
    ACCEPTED_BCFTOOLS_DIGEST,
    ACCEPTED_HAPPY_DIGEST,
    HARNESS_READY_GATE_HASH,
    HARNESS_READY_QUALIFICATION_HASH,
)

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

    harness_ready_gate_hash: str = Field(min_length=64, max_length=64)
    harness_ready_qualification_hash: str = Field(min_length=64, max_length=64)
    harness_ready_gate_verified: bool
    objective_identity: str = Field(min_length=64, max_length=64)
    candidate_design_identity: str = Field(min_length=64, max_length=64)
    descends_closure_authority_source: bool

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
    from minos_engine.qualification.l2f2_baseline_qualified_contract import (
        candidate_design_identity,
        objective_identity,
    )

    checks: dict[str, bool] = {}

    # ---- SOURCE ----------------------------------------------------------------------------
    checks["qualified_source_present"] = (
        bool(observation.qualified_source_commit) and observation.descends_closure_authority_source
    )
    checks["qualified_source_tree_matches"] = (
        observation.worktree_tree == observation.qualified_source_tree
    )
    checks["worktree_matches_qualified_source"] = (
        observation.worktree_commit == observation.qualified_source_commit
        and observation.worktree_clean
    )

    # ---- PREREQUISITES ---------------------------------------------------------------------
    # FULL equality, and the committed gate must actually have verified.
    checks["harness_ready_gate_bound"] = (
        observation.harness_ready_gate_hash == HARNESS_READY_GATE_HASH
        and observation.harness_ready_gate_verified
    )
    checks["harness_ready_qualification_bound"] = (
        observation.harness_ready_qualification_hash == HARNESS_READY_QUALIFICATION_HASH
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
    # The objective identity is DERIVED from the exact frozen protocol sub-blocks that define
    # it, then compared to what the observer derived. Constants alone would not notice a changed
    # aggregation-utility rule, a changed failure denominator or a reordered tie-break.
    protocol_content = dict(committed_protocol.get("content") or {})
    checks["objective_authority_exact"] = observation.objective_identity == objective_identity(
        protocol_content
    ) and (CVAR_ALPHA, CVAR_WEIGHT, FLOOR_WEIGHT, MEAN_WEIGHT, FAILURE_PENALTY) == (
        0.25,
        0.50,
        0.30,
        0.20,
        1.00,
    )
    checks["candidate_design_authority_exact"] = (
        observation.candidate_design_identity == candidate_design_identity(protocol_content)
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
    # the EXACT audited images. An arbitrary immutable-looking digest is not this scorer.
    checks["happy_immutable_digest_exact"] = observation.happy_digest == ACCEPTED_HAPPY_DIGEST
    checks["bcftools_immutable_digest_exact"] = (
        observation.bcftools_digest == ACCEPTED_BCFTOOLS_DIGEST
    )

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
    # EXACT, not merely present. Six non-empty strings could be six unrelated files.
    from minos_engine.qualification.l2f2_baseline_qualified_qualifier import (
        ACCEPTED_EVIDENCE_SHA256,
    )

    checks["evidence_hashes_complete"] = {
        name: observation.evidence_hashes.get(name) for name in ACCEPTED_EVIDENCE_SHA256
    } == dict(ACCEPTED_EVIDENCE_SHA256) and not (
        set(observation.evidence_hashes)
        - set(ACCEPTED_EVIDENCE_SHA256)
        - {"scoring_contract_hash", "minos_subnet_sha", "execution_environment_hash"}
    )
    return checks


BASELINE_QUALIFIED_GATE_PATH: Final = "gates/baseline-qualified.json"
BASELINE_QUALIFICATION_RESULT_PATH: Final = "reports/layer2/baseline-qualified-result.json"


def observation_from_result(result: BaselineQualificationResult) -> BaselineQualifiedObservation:
    """Project a VERIFIED qualification result onto the observation the checks read.

    A pure projection: it adds nothing and decides nothing, so what the gate checks is exactly
    what the qualifier measured.
    """
    from minos_engine.qualification.l2f2_train_evidence import verify_train_evidence

    return BaselineQualifiedObservation(
        qualified_source_commit=result.qualified_source_git_sha,
        qualified_source_tree=result.qualified_source_tree_sha,
        worktree_commit=result.qualified_source_git_sha,
        worktree_tree=result.qualified_source_tree_sha,
        worktree_clean=result.worktree_clean,
        descends_closure_authority_source=result.descends_closure_authority_source,
        harness_ready_gate_hash=result.harness_ready_gate_hash,
        harness_ready_qualification_hash=result.harness_ready_qualification_hash,
        harness_ready_gate_verified=result.harness_ready_gate_verified,
        objective_identity=result.objective_identity,
        candidate_design_identity=result.candidate_design_identity,
        closure_artifact_verified=result.closure_artifact_verified,
        closure_hash_recomputed=result.phase_d_closure_hash,
        baseline_selected_hash=result.baseline_selected_hash,
        baseline_selected_manifest_verified=result.baseline_selected_manifest_verified,
        candidate_count=result.candidate_count,
        member_count=result.member_count,
        observation_count=result.observation_count,
        all_candidates_complete=result.all_candidates_complete,
        validation_infrastructure_incidents=result.validation_infrastructure_incidents,
        selected_config_hash=result.selected_config_hash,
        selected_rank=result.selected_rank,
        selected_inherited_candidate_index=result.selected_inherited_candidate_index,
        selected_statistics_agree=result.selected_statistics_verified,
        seed_config_hash=result.seed_config_hash,
        seed_rank=result.seed_rank,
        scorer_source_identities_exact=result.scorer_source_identities_verified,
        happy_digest=result.happy_resolved_digest,
        bcftools_digest=result.bcftools_resolved_digest,
        train=verify_train_evidence(result.train.as_observed()),
        test_untouched=result.test_untouched,
        train_and_validation_identities_disjoint=(result.train_and_validation_identities_disjoint),
        evidence_hashes={
            "scoring_contract_hash": result.scoring_contract_hash,
            "minos_subnet_sha": result.minos_subnet_sha,
            "execution_environment_hash": result.execution_environment_hash,
            **result.evidence_sha256,
        },
    )


def assemble_baseline_qualified_gate(
    trusted: TrustedBaselineQualification, *, created_at: str | None = None
) -> Any:
    """Assemble the canonical :class:`GateArtifact`. Nothing about it is caller-nominable.

    ``evidence`` used to be a caller parameter. It is gone: a caller able to inject evidence items
    could have placed an external-CI assertion inside the scientific authority, which is precisely
    what this gate must never carry. The authority is the canonical ``input_hashes`` plus the
    qualification artifact, both derived from the trusted result.
    """
    from datetime import UTC, datetime

    from minos_engine.gates.contracts import GateArtifact, GateStatus
    from minos_engine.gates.required_checks import required_checks_for
    from minos_engine.qualification.l2f2_baseline_qualified_contract import (
        BASELINE_QUALIFICATION_TOOL_VERSION,
        compute_baseline_qualification_hash,
    )

    if not isinstance(trusted, TrustedBaselineQualification):
        raise BaselineQualificationError(
            "only a TrustedBaselineQualification may assemble a BASELINE-QUALIFIED gate"
        )
    result = trusted.result
    checks = derive_checks(observation_from_result(result))
    required = required_checks_for(BASELINE_QUALIFIED_GATE)
    if set(checks) != required:
        raise BaselineQualificationError(
            f"derived checks are not the registered set: "
            f"missing {sorted(required - set(checks))}, extra {sorted(set(checks) - required)}"
        )
    status = GateStatus.PASS if all(checks.values()) else GateStatus.HOLD
    return GateArtifact(
        gate_name=BASELINE_QUALIFIED_GATE,
        status=status,
        engine_git_sha=result.qualified_source_git_sha,
        qualified_source_git_sha=result.qualified_source_git_sha,
        qualified_source_tree_sha=result.qualified_source_tree_sha,
        qualification_tool_version=BASELINE_QUALIFICATION_TOOL_VERSION,
        input_hashes={
            "qualification_hash": compute_baseline_qualification_hash(result),
            "baseline_selected_hash": result.baseline_selected_hash,
            "phase_d_closure_hash": result.phase_d_closure_hash,
            "baseline_protocol_hash": result.baseline_protocol_hash,
            "selection_interpretation_hash": result.selection_interpretation_hash,
            "scoring_contract_hash": result.scoring_contract_hash,
            "objective_identity": result.objective_identity,
            "candidate_design_identity": result.candidate_design_identity,
        },
        evidence=(),
        mandatory_checks=checks,
        created_at=created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def write_baseline_qualification_outputs(
    gate: Any, result: BaselineQualificationResult, *, root: Path | None = None
) -> tuple[Path, Path]:
    """Write the gate and the canonical qualification bytes. The EVIDENCE step calls this."""

    from minos_engine.gates.verifier import write_gate
    from minos_engine.qualification.l2f2_baseline_qualified_contract import (
        canonical_baseline_qualification_bytes,
        compute_baseline_qualification_hash,
    )
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    # publishing a gate beside a qualification it does not describe would create durable evidence
    # that verifies against nothing. Refuse before writing, not after.
    expected = compute_baseline_qualification_hash(result)
    if gate.input_hashes.get("qualification_hash") != expected:
        raise BaselineQualificationError(
            "refusing to publish: the gate's qualification_hash does not describe this result"
        )
    if (gate.qualified_source_git_sha, gate.qualified_source_tree_sha) != (
        result.qualified_source_git_sha,
        result.qualified_source_tree_sha,
    ):
        raise BaselineQualificationError(
            "refusing to publish: the gate names a different qualified source than the result"
        )
    if gate.input_hashes.get("baseline_selected_hash") != result.baseline_selected_hash:
        raise BaselineQualificationError(
            "refusing to publish: the gate names a different baseline-selected authority"
        )

    base = root or repository_root()
    gate_path = write_gate(gate, base / BASELINE_QUALIFIED_GATE_PATH)
    result_path = base / BASELINE_QUALIFICATION_RESULT_PATH
    result_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(canonical_baseline_qualification_bytes(result))
    if result.created_at is not None:
        payload["created_at"] = result.created_at
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return gate_path, result_path


def verify_baseline_qualified_gate(
    *, gate_path: str | Path, qualification_path: str | Path, root: Path | None = None
) -> dict[str, Any]:
    """Verify a published gate offline through the canonical framework.

    No database, no GATK, no scorer, no truth. The stored qualification is parsed with its strict
    model, its hash recomputed from its own bytes and matched to the gate's
    ``input_hashes["qualification_hash"]``, and the 42 checks RE-DERIVED from it — a stored
    ``checks`` dictionary is never treated as authority. Git provenance is proven with the
    repository's own helpers rather than by inspecting the shape of a 40-character string.
    """

    from minos_engine.baseline.baseline_selected import (
        BaselineSelectedError,
        compute_baseline_selected_hash,
        load_committed_baseline_selected,
    )
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.gates.required_checks import required_checks_for
    from minos_engine.gates.verifier import load_gate, verify_gate_integrity
    from minos_engine.qualification import git_tree
    from minos_engine.qualification.l2f2_baseline_qualified_contract import (
        BASELINE_QUALIFICATION_TOOL_VERSION,
        BaselineQualificationResult,
        compute_baseline_qualification_hash,
    )
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    reasons: list[str] = []
    base = root or repository_root()

    try:
        gate = load_gate(gate_path)
    except Exception as exc:
        return {
            "gate_name": BASELINE_QUALIFIED_GATE,
            "ok": False,
            "reasons": [f"gate artifact is unusable: {exc}"],
        }
    integrity = verify_gate_integrity(gate)
    if not integrity.ok:
        reasons.extend(str(r) for r in (integrity.reasons or ("gate integrity failed",)))
    if gate.gate_name != BASELINE_QUALIFIED_GATE:
        reasons.append(f"gate names {gate.gate_name!r}")
    if gate.status is not GateStatus.PASS:
        reasons.append(f"gate status is {gate.status}")
    if gate.qualification_tool_version != BASELINE_QUALIFICATION_TOOL_VERSION:
        reasons.append(f"qualification tool version is {gate.qualification_tool_version!r}")

    target = Path(qualification_path)
    if target.is_symlink() or not target.is_file():
        reasons.append(f"qualification artifact {target} is missing or a symlink")
        return {"gate_name": BASELINE_QUALIFIED_GATE, "ok": False, "reasons": reasons}
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
        # the contract is strict and plan_hashes is a tuple; canonical JSON necessarily writes it
        # as an array, so it is restored rather than the model loosened.
        train = stored.get("train")
        if isinstance(train, dict) and isinstance(train.get("plan_hashes"), list):
            stored = {**stored, "train": {**train, "plan_hashes": tuple(train["plan_hashes"])}}
        result = BaselineQualificationResult.model_validate(stored)
    except Exception as exc:
        reasons.append(f"qualification artifact is not a valid qualification result: {exc}")
        return {"gate_name": BASELINE_QUALIFIED_GATE, "ok": False, "reasons": reasons}

    recomputed = compute_baseline_qualification_hash(result)
    if gate.input_hashes.get("qualification_hash") != recomputed:
        reasons.append(
            "gate qualification_hash does not match the qualification artifact's own bytes"
        )

    # RE-DERIVE. A stored checks dictionary is a convenience, never an authority.
    required = required_checks_for(BASELINE_QUALIFIED_GATE)
    derived = derive_checks(observation_from_result(result))
    if set(derived) != required:
        reasons.append("re-derived checks are not the registered set")
    if derived != gate.mandatory_checks:
        differing = sorted(
            name
            for name in set(derived) | set(gate.mandatory_checks)
            if derived.get(name) != gate.mandatory_checks.get(name)
        )
        reasons.append(f"gate checks disagree with re-derived checks: {differing}")
    failed = sorted(name for name, ok in derived.items() if not ok)
    if failed:
        reasons.append(f"mandatory checks derive false: {failed}")

    for field, got, want in (
        (
            "qualified_source_git_sha",
            gate.qualified_source_git_sha,
            result.qualified_source_git_sha,
        ),
        (
            "qualified_source_tree_sha",
            gate.qualified_source_tree_sha,
            result.qualified_source_tree_sha,
        ),
    ):
        if got != want:
            reasons.append(f"gate and qualification disagree on {field}")

    # ---- real Git provenance, not string shape ---------------------------------------------
    if git_tree.is_git_repo(base):
        commit = gate.qualified_source_git_sha or ""
        if not git_tree.is_commit(base, commit):
            reasons.append(f"qualified source commit {commit} does not exist in this repository")
        else:
            actual_tree = git_tree.commit_tree_sha(base, commit)
            if actual_tree != gate.qualified_source_tree_sha:
                reasons.append(
                    f"qualified source commit's actual tree is {actual_tree}, but the gate names "
                    f"{gate.qualified_source_tree_sha}"
                )
            if not git_tree.is_ancestor(base, CLOSURE_AUTHORITY_SOURCE, commit):
                reasons.append(
                    "the qualified source does not descend the accepted Phase-D closure authority "
                    f"source {CLOSURE_AUTHORITY_SOURCE}"
                )
    else:
        reasons.append("git provenance could not be verified: not a git repository")

    # exact immutable identities, recomputed offline rather than trusted from the artifact
    from minos_engine.evaluation.scoring_contract import (
        compute_scoring_contract_hash,
        load_scoring_authority,
    )
    from minos_engine.qualification.l2f2_baseline_qualified_contract import (
        ACCEPTED_BCFTOOLS_DIGEST,
        ACCEPTED_HAPPY_DIGEST,
        HARNESS_READY_GATE_HASH,
        HARNESS_READY_QUALIFICATION_HASH,
        candidate_design_identity,
        objective_identity,
    )
    from minos_engine.qualification.l2f2_baseline_qualified_qualifier import (
        ACCEPTED_EVIDENCE_SHA256,
        ACCEPTED_MINOS_SUBNET_SHA,
        ACCEPTED_SCORING_PY_SHA256,
        ACCEPTED_TOOL_PARAMS_PY_SHA256,
        ACCEPTED_VALIDATOR_PY_SHA256,
    )

    try:
        protocol_content = dict(
            (
                __import__("minos_engine.baseline.protocol", fromlist=["load_committed_protocol"])
                .load_committed_protocol(base)
                .get("content")
            )
            or {}
        )
    except Exception as exc:
        reasons.append(f"the committed protocol is unusable: {exc}")
    else:
        if result.objective_identity != objective_identity(protocol_content):
            reasons.append("objective_identity does not match the committed protocol")
        if result.candidate_design_identity != candidate_design_identity(protocol_content):
            reasons.append("candidate_design_identity does not match the committed protocol")

    try:
        authority = load_scoring_authority(base)
    except Exception as exc:
        reasons.append(f"the committed scoring authority is unusable: {exc}")
    else:
        for label, got, want in (
            (
                "scoring contract",
                compute_scoring_contract_hash(authority),
                result.scoring_contract_hash,
            ),
            ("MINOS_SUBNET", authority.upstream_commit, ACCEPTED_MINOS_SUBNET_SHA),
            ("hap.py digest", authority.happy.resolved_digest, ACCEPTED_HAPPY_DIGEST),
            ("bcftools digest", authority.bcftools.resolved_digest, ACCEPTED_BCFTOOLS_DIGEST),
            ("scoring.py", authority.scoring_py_sha256, ACCEPTED_SCORING_PY_SHA256),
            ("validator.py", authority.validator_py_sha256, ACCEPTED_VALIDATOR_PY_SHA256),
            ("tool_params.py", authority.tool_params_py_sha256, ACCEPTED_TOOL_PARAMS_PY_SHA256),
        ):
            if got != want:
                reasons.append(f"committed scorer {label} is {got}, expected {want}")

    if result.harness_ready_gate_hash != HARNESS_READY_GATE_HASH:
        reasons.append("qualification names a different HARNESS gate identity")
    if result.harness_ready_qualification_hash != HARNESS_READY_QUALIFICATION_HASH:
        reasons.append("qualification names a different HARNESS qualification identity")
    if dict(result.evidence_sha256) != dict(ACCEPTED_EVIDENCE_SHA256):
        reasons.append("qualification evidence hashes are not the accepted six")

    try:
        committed = load_committed_baseline_selected(base)
    except BaselineSelectedError as exc:
        reasons.append(f"committed baseline-selected authority is unusable: {exc}")
    else:
        expected = compute_baseline_selected_hash()
        if committed.get("baseline_selected_hash") != expected:
            reasons.append("committed baseline-selected hash disagrees with source")
        if gate.input_hashes.get("baseline_selected_hash") != expected:
            reasons.append("gate baseline-selected hash disagrees with the committed authority")

    return {
        "gate_name": BASELINE_QUALIFIED_GATE,
        "ok": not reasons,
        "reasons": reasons,
        "required_check_count": len(required),
    }
