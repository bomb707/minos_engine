"""L2-F F7 HARNESS-READY qualifier: derive every check from real observations.

This module can *assemble* and *verify* a HARNESS-READY gate, and can *run* the live
qualification. It never accepts a caller-supplied dictionary of booleans, scientific hash, plan,
candidate set, runner or "the tests passed" assertion: every check in
:data:`HARNESS_READY_REQUIRED_CHECKS` is derived from an immutable
:class:`HarnessReadyQualification` observation set.

F7-A ships the framework only. Running the live qualification is an explicit, operator-invoked
action, and the resulting ``gates/harness-ready.json`` belongs to the later F7-B commit — a source
commit cannot truthfully bind its own final commit/tree before that commit exists.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any

from minos_engine.common.errors import MinosEngineError
from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import load_gate, verify_gate_integrity, write_gate
from minos_engine.qualification.git_tree import (
    commit_tree_sha,
    is_ancestor,
    is_commit,
    is_git_repo,
    repo_root,
)
from minos_engine.qualification.l2f_failure_inventory import verify_failure_inventory
from minos_engine.qualification.l2f_harness_ready_contract import (
    ACCEPTED_CANDIDATE_COUNT,
    ACCEPTED_CANDIDATE_SET_HASH,
    ACCEPTED_F5_CONTRACT_HASH,
    ACCEPTED_F6_CORRECTIVE_COMMIT,
    ACCEPTED_LOGICAL_JOB_COUNT,
    ACCEPTED_MIGRATION_SHAS,
    ACCEPTED_PARAMETER_SPACE_HASH,
    ACCEPTED_PLAN_HASH,
    HARNESS_READY_GATE,
    HARNESS_READY_GATE_PATH,
    HARNESS_READY_QUALIFIER_SCHEMA,
    HARNESS_READY_QUALIFIER_VERSION,
    HarnessReadyQualification,
    canonical_qualification_bytes,
    compute_qualification_hash,
    load_qualification_json,
)

__all__ = [
    "HARNESS_READY_GATE",
    "HARNESS_READY_GATE_PATH",
    "HARNESS_READY_REQUIRED_CHECKS",
    "OPERATIONAL_DATABASE_NAME",
    "HarnessReadyQualificationError",
    "OperationalDatabaseRefused",
    "refuse_operational_database",
    "derive_checks",
    "assemble_harness_ready_gate",
    "write_qualification_outputs",
    "verify_committed_harness_ready_gate",
]

#: the canonical operational store F7 must never qualify against.
OPERATIONAL_DATABASE_NAME = "minos_engine_db"

#: the complete HARNESS-READY required-check set (mirrors gates.required_checks).
HARNESS_READY_REQUIRED_CHECKS: tuple[str, ...] = (
    # source provenance + accepted ancestry
    "qualified_source_present",
    "qualified_source_tree_matches",
    "source_descends_f6_corrective",
    "worktree_matches_qualified_source",
    # accepted identities (recomputed, never asserted)
    "accepted_e5_gates_bound",
    "accepted_migrations_unchanged",
    "accepted_f5_contract_bound",
    "accepted_parameter_space_bound",
    "accepted_policy_hash_bound",
    "accepted_candidate_set_bound",
    "accepted_plan_identity_bound",
    "alembic_head_is_0008",
    # official GATK execution + Twin parity
    "official_gatk_runner_used",
    "official_gatk_binary_pinned",
    "official_execution_succeeded",
    "official_execution_artifacts_published",
    "gatk_twin_semantic_parity",
    "gatk_only_policy",
    # idempotent resume
    "resume_after_restart_verified",
    "resume_creates_no_duplicates",
    "resume_preserves_terminal_jobs",
    "resume_conflicting_replay_rejected",
    "resume_exhausted_queue_returns_none",
    "no_stranded_jobs",
    "no_automatic_retry",
    # independent artifact + result verification
    "artifact_bytes_independently_verified",
    "content_addressed_names_verified",
    "media_types_verified",
    "input_identity_recomputed",
    "logical_argv_recomputed",
    "result_hash_recomputed",
    "harness_verifier_all_checks_pass",
    "verification_non_mutating",
    # typed failure classification
    "failure_inventory_complete",
    "failure_inventory_unambiguous",
    # leakage + authority boundaries
    "no_truth_or_scoring_access",
    "train_partition_only",
    "operational_database_untouched",
    "select_config_still_blocked",
    "no_network_access",
)


def _now_iso() -> str:
    """Canonical timezone-aware UTC stamp (excluded from every identity hash)."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


class HarnessReadyQualificationError(MinosEngineError):
    """The HARNESS-READY qualification could not be produced or verified."""


class OperationalDatabaseRefused(HarnessReadyQualificationError):
    """F7 qualification refused to run against the operational database."""


def refuse_operational_database(database_url: str | None = None) -> None:
    """Fail closed when the qualification would target the canonical operational store.

    F7 qualification is a rehearsal: it must run only against isolated scratch PostgreSQL. This
    never weakens the production identity checks — it is an ADDITIONAL refusal on top of them.
    """
    url = database_url if database_url is not None else os.environ.get("MINOS_DATABASE_URL")
    if not url:
        return
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if tail == OPERATIONAL_DATABASE_NAME:
        raise OperationalDatabaseRefused(
            f"F7 qualification refuses the operational database {OPERATIONAL_DATABASE_NAME!r}; "
            "qualification runs only against isolated scratch PostgreSQL"
        )


# --------------------------------------------------------------------------- #
# check derivation — every value comes from an OBSERVATION, never from a caller
# --------------------------------------------------------------------------- #
def derive_checks(result: HarnessReadyQualification) -> dict[str, bool]:
    """Derive the complete required-check map from an immutable qualification observation set."""
    src = result.source
    acc = result.accepted
    ex = result.official_execution
    par = result.twin_parity
    res = result.resume
    ver = result.artifact_verification
    inv = result.failure_inventory
    bnd = result.boundaries

    checks: dict[str, bool] = {
        "qualified_source_present": bool(src.qualified_source_git_sha),
        "qualified_source_tree_matches": bool(src.qualified_source_tree_sha),
        "source_descends_f6_corrective": (
            src.descends_f6_corrective and src.f6_corrective_commit == ACCEPTED_F6_CORRECTIVE_COMMIT
        ),
        "worktree_matches_qualified_source": src.worktree_matches_qualified_source,
        "accepted_e5_gates_bound": bool(acc.e5_gate_hashes)
        and all(len(v) == 64 for v in acc.e5_gate_hashes.values()),
        "accepted_migrations_unchanged": acc.migration_sha256 == ACCEPTED_MIGRATION_SHAS,
        "accepted_f5_contract_bound": acc.f5_contract_hash == ACCEPTED_F5_CONTRACT_HASH,
        "accepted_parameter_space_bound": (
            acc.parameter_space_hash == ACCEPTED_PARAMETER_SPACE_HASH
        ),
        "accepted_policy_hash_bound": bool(acc.policy_hash),
        "accepted_candidate_set_bound": (
            acc.candidate_set_hash == ACCEPTED_CANDIDATE_SET_HASH
            and acc.candidate_count == ACCEPTED_CANDIDATE_COUNT
        ),
        "accepted_plan_identity_bound": (
            acc.plan_hash == ACCEPTED_PLAN_HASH
            and acc.logical_job_count == ACCEPTED_LOGICAL_JOB_COUNT
        ),
        "alembic_head_is_0008": acc.alembic_head == "0008_l2f_execution_results",
        "official_gatk_runner_used": (
            ex.used_official_runner and ex.runner_class == "SubprocessGatkRunner"
        ),
        "official_gatk_binary_pinned": (
            not result.gatk_binary.absolute_path_is_symlink
            and len(result.gatk_binary.executable_sha256) == 64
            and bool(result.gatk_binary.version)
        ),
        "official_execution_succeeded": ex.job_status == "SUCCEEDED",
        "official_execution_artifacts_published": ex.published_artifact_count == 2,
        "gatk_twin_semantic_parity": par.parity_ok and par.first_difference is None,
        "gatk_only_policy": par.caller == "gatk" and par.subcommand == "HaplotypeCaller",
        "resume_after_restart_verified": res.engines_recreated,
        "resume_creates_no_duplicates": res.duplicate_rows_created == 0,
        "resume_preserves_terminal_jobs": (
            not res.terminal_job_reset
            and not res.terminal_job_reexecuted
            and not res.artifact_bytes_rewritten
            and res.exact_replay_returned_existing
        ),
        "resume_conflicting_replay_rejected": res.conflicting_replay_rejected,
        "resume_exhausted_queue_returns_none": res.exhausted_queue_returns_none,
        "no_stranded_jobs": res.nonterminal_jobs_remaining == 0,
        "no_automatic_retry": not res.automatic_retry_observed,
        "artifact_bytes_independently_verified": (
            ver.config_artifact_ok and ver.vcf_artifact_ok and ver.result_manifest_artifact_ok
        ),
        "content_addressed_names_verified": ver.content_addressed_names_ok,
        "media_types_verified": ver.media_types_ok,
        "input_identity_recomputed": (
            ver.recomputed_input_identity_hash == result.qualification_input.input_identity_hash
        ),
        "logical_argv_recomputed": ver.recomputed_logical_argv_hash == ex.logical_argv_hash,
        "result_hash_recomputed": ver.recomputed_result_hash == ex.result_hash,
        "harness_verifier_all_checks_pass": (
            ver.harness_verifier_status == "PASS"
            and bool(ver.harness_verifier_checks)
            and all(ver.harness_verifier_checks.values())
        ),
        "verification_non_mutating": (
            ver.verifier_non_mutating and ver.fingerprint_before == ver.fingerprint_after
        ),
        "failure_inventory_complete": inv.complete,
        "failure_inventory_unambiguous": inv.unambiguous,
        "no_truth_or_scoring_access": (
            bnd.truth_paths_resolved == 0 and bnd.scoring_paths_resolved == 0
        ),
        "train_partition_only": (
            bnd.nontrain_members_touched == 0 and result.qualification_input.partition == "train"
        ),
        "operational_database_untouched": (
            not bnd.operational_database_written
            and bnd.operational_database_revision == "0005_l2e_feature_view"
            and bnd.operational_l2f_table_count == 0
        ),
        "select_config_still_blocked": bnd.select_config_blocked,
        "no_network_access": not bnd.network_access_performed,
    }
    missing = set(HARNESS_READY_REQUIRED_CHECKS) - set(checks)
    if missing:  # pragma: no cover - structural guard
        raise HarnessReadyQualificationError(f"derive_checks omitted required checks: {missing}")
    return checks


def assemble_harness_ready_gate(
    result: HarnessReadyQualification,
    *,
    created_at: str | None = None,
    evidence: tuple[EvidenceItem, ...] = (),
) -> GateArtifact:
    """Deterministically assemble the HARNESS-READY gate from a qualification result.

    The status is DERIVED: PASS only when every required check derived from the observations is
    true. A caller cannot request PASS, and cannot inject a check value.
    """
    verify_failure_inventory(result.failure_inventory)
    checks = derive_checks(result)
    required = required_checks_for(HARNESS_READY_GATE)
    if not required:  # pragma: no cover - registered at import time
        raise HarnessReadyQualificationError("HARNESS-READY is not a registered gate name")
    missing = sorted(required - set(checks))
    if missing:
        raise HarnessReadyQualificationError(f"derived checks are missing: {missing}")
    status = GateStatus.PASS if all(checks[name] for name in required) else GateStatus.HOLD
    return GateArtifact(
        gate_name=HARNESS_READY_GATE,
        status=status,
        engine_git_sha=result.source.qualified_source_git_sha,
        input_hashes={
            "qualification_hash": compute_qualification_hash(result),
            "parameter_space_hash": result.accepted.parameter_space_hash,
            "candidate_set_hash": result.accepted.candidate_set_hash,
            "plan_hash": result.accepted.plan_hash,
            "f5_contract_hash": result.accepted.f5_contract_hash,
            "result_hash": result.official_execution.result_hash,
            "twin_plan_hash": result.twin_parity.twin_plan_hash,
        },
        evidence=evidence,
        mandatory_checks=checks,
        qualified_source_git_sha=result.source.qualified_source_git_sha,
        qualified_source_tree_sha=result.source.qualified_source_tree_sha,
        qualification_tool_version=(
            f"{HARNESS_READY_QUALIFIER_SCHEMA}/{HARNESS_READY_QUALIFIER_VERSION}"
        ),
        created_at=created_at or _now_iso(),
    )


def write_qualification_outputs(
    result: HarnessReadyQualification,
    gate: GateArtifact,
    *,
    gate_path: str | Path,
    qualification_path: str | Path,
) -> tuple[Path, Path]:
    """Write the gate and the canonical qualification result (explicit F7-B step only)."""
    gate_file = write_gate(gate, gate_path)
    qualification_file = Path(qualification_path)
    qualification_file.parent.mkdir(parents=True, exist_ok=True)
    qualification_file.write_bytes(canonical_qualification_bytes(result) + b"\n")
    return gate_file, qualification_file


# --------------------------------------------------------------------------- #
# offline verification of an already-committed gate (no GATK, no database)
# --------------------------------------------------------------------------- #
def verify_committed_harness_ready_gate(
    *,
    base_dir: str | Path = ".",
    gate_path: str | Path = HARNESS_READY_GATE_PATH,
    qualification_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify an already-committed HARNESS-READY gate offline. Fails closed on every deficiency.

    Runs no GATK, opens no database and never runs Alembic.
    """
    root = Path(base_dir).resolve()
    reasons: list[str] = []

    path = Path(gate_path)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        return {
            "gate_name": HARNESS_READY_GATE,
            "ok": False,
            "reasons": [f"missing evidence: {gate_path} has not been generated (F7-B)"],
        }

    gate = load_gate(path)
    if gate.gate_name != HARNESS_READY_GATE:
        reasons.append(f"gate_name is {gate.gate_name!r}, expected {HARNESS_READY_GATE!r}")
    integrity = verify_gate_integrity(gate, base_dir=root)
    if not integrity.ok:
        reasons.extend(integrity.reasons)
    if gate.status is not GateStatus.PASS:
        reasons.append(f"status is {gate.status.value}, not PASS")

    required = required_checks_for(HARNESS_READY_GATE)
    missing = sorted(required - set(gate.mandatory_checks))
    if missing:
        reasons.append(f"missing required checks: {missing}")
    false_checks = sorted(k for k, v in gate.mandatory_checks.items() if not v)
    if false_checks:
        reasons.append(f"failed checks: {false_checks}")

    # git history: the qualified source must exist and descend from the accepted F6 corrective.
    git_root = repo_root(root) or root
    if not is_git_repo(git_root):
        reasons.append("missing Git history: not a git repository")
    else:
        source = gate.qualified_source_git_sha
        if not source or not is_commit(git_root, source):
            reasons.append(f"qualified source commit is absent from history: {source!r}")
        else:
            tree = commit_tree_sha(git_root, source)
            if tree != gate.qualified_source_tree_sha:
                reasons.append(
                    f"wrong source tree: commit {source} has tree {tree}, "
                    f"gate binds {gate.qualified_source_tree_sha}"
                )
            if not is_ancestor(git_root, ACCEPTED_F6_CORRECTIVE_COMMIT, source):
                reasons.append(
                    f"qualified source {source} does not descend the accepted F6 corrective "
                    f"{ACCEPTED_F6_CORRECTIVE_COMMIT}"
                )

    # optional: the canonical qualification bytes must reproduce the bound qualification hash.
    if qualification_path is not None:
        qpath = Path(qualification_path)
        if not qpath.is_absolute():
            qpath = root / qpath
        if not qpath.exists():
            reasons.append(f"missing qualification result: {qualification_path}")
        else:
            try:
                parsed = load_qualification_json(qpath.read_bytes().rstrip(b"\n"))
            except MinosEngineError as exc:
                reasons.append(f"qualification result is invalid: {exc}")
            else:
                expected = gate.input_hashes.get("qualification_hash")
                actual = compute_qualification_hash(parsed)
                if expected != actual:
                    reasons.append(
                        f"qualification hash mismatch: gate binds {expected}, bytes yield {actual}"
                    )
                derived = derive_checks(parsed)
                if {k: gate.mandatory_checks.get(k) for k in derived} != derived:
                    reasons.append("gate checks do not equal the checks derived from the result")

    return {
        "gate_name": HARNESS_READY_GATE,
        "ok": not reasons,
        "gate_hash": gate.gate_hash,
        "status": gate.status.value,
        "reasons": tuple(reasons),
    }
