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
from minos_engine.experiments.execution_contract import compute_gatk_runtime_bundle_sha256
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
from minos_engine.qualification.l2f_accepted_identities import (
    recompute_accepted_identities,
    verify_accepted_identities,
)
from minos_engine.qualification.l2f_failure_inventory import verify_failure_inventory
from minos_engine.qualification.l2f_harness_ready_contract import (
    ACCEPTED_CANDIDATE_COUNT,
    ACCEPTED_CANDIDATE_SET_HASH,
    ACCEPTED_F5_CONTRACT_HASH,
    ACCEPTED_F6_CORRECTIVE_COMMIT,
    ACCEPTED_LIVE_GATK_PARAMETER_SPACE_ARTIFACT_SHA256,
    ACCEPTED_LIVE_GATK_SOURCE_ARTIFACT_SHA256,
    ACCEPTED_LOGICAL_JOB_COUNT,
    ACCEPTED_MIGRATION_SHAS,
    ACCEPTED_PARAMETER_SPACE_HASH,
    ACCEPTED_PLAN_HASH,
    ACCEPTED_POLICY_HASH,
    HARNESS_READY_GATE,
    HARNESS_READY_GATE_PATH,
    HARNESS_READY_QUALIFICATION_TOOL_VERSION,
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
    "assemble_gate_from_trusted_qualification",
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
def derive_checks(
    result: HarnessReadyQualification,
    *,
    recomputed_accepted: Any = None,
    root: Path | None = None,
) -> dict[str, bool]:
    """Derive the complete required-check map from an immutable qualification observation set.

    ``recomputed_accepted``/``root`` make the accepted-identity closure explicit about WHICH
    checkout it recomputes from. Offline verification of another checkout must pass its own root,
    so the closure can never silently fall back to this module's repository.
    """
    src = result.source
    acc = result.accepted
    ex = result.official_execution
    par = result.twin_parity
    res = result.resume
    ver = result.artifact_verification
    inv = result.failure_inventory
    bnd = result.boundaries
    # every accepted identity is compared against values RECOMPUTED from the committed bytes,
    # never against a shape check such as bool(...) or "is 64 hex characters".
    recomputed = (
        recomputed_accepted
        if recomputed_accepted is not None
        else recompute_accepted_identities(root)
    )

    checks: dict[str, bool] = {
        "qualified_source_present": bool(src.qualified_source_git_sha),
        "qualified_source_tree_matches": bool(src.qualified_source_tree_sha),
        "source_descends_f6_corrective": (
            src.descends_f6_corrective and src.f6_corrective_commit == ACCEPTED_F6_CORRECTIVE_COMMIT
        ),
        "worktree_matches_qualified_source": src.worktree_matches_qualified_source,
        "accepted_e5_gates_bound": acc.e5_gate_hashes == recomputed.e5_gate_hashes,
        "accepted_migrations_unchanged": (
            acc.migration_sha256 == ACCEPTED_MIGRATION_SHAS
            and acc.migration_sha256 == recomputed.migration_sha256
        ),
        "accepted_f5_contract_bound": acc.f5_contract_hash == ACCEPTED_F5_CONTRACT_HASH,
        "accepted_parameter_space_bound": (
            acc.parameter_space_hash == ACCEPTED_PARAMETER_SPACE_HASH
            and acc.parameter_space_hash == recomputed.parameter_space_hash
            and acc.live_gatk_source_artifact_sha256 == ACCEPTED_LIVE_GATK_SOURCE_ARTIFACT_SHA256
            and acc.live_gatk_parameter_space_artifact_sha256
            == ACCEPTED_LIVE_GATK_PARAMETER_SPACE_ARTIFACT_SHA256
            and acc.live_gatk_source_artifact_sha256 == recomputed.live_gatk_source_artifact_sha256
            and acc.live_gatk_parameter_space_artifact_sha256
            == recomputed.live_gatk_parameter_space_artifact_sha256
        ),
        "accepted_policy_hash_bound": (
            acc.policy_hash == ACCEPTED_POLICY_HASH and acc.policy_hash == recomputed.policy_hash
        ),
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
        # the launcher alone is a dispatcher: the pinned identity must bind the local JAR the
        # launcher actually runs, and the observed runtime version must equal the provisioned one.
        "official_gatk_binary_pinned": (
            not result.gatk_binary.absolute_path_is_symlink
            and not result.gatk_binary.local_jar_is_symlink
            and not result.gatk_binary.jar_override_variables_inherited
            and len(result.gatk_binary.executable_sha256) == 64
            and result.gatk_binary.local_jar_sha256 is not None
            and result.gatk_binary.runtime_bundle_sha256 is not None
            and result.gatk_binary.python_executable_sha256 is not None
            and result.gatk_binary.java_executable_sha256 is not None
            and bool(result.gatk_binary.version)
            and result.gatk_binary.observed_version == result.gatk_binary.version
            # the recorded bundle digest must actually derive from the recorded parts
            and result.gatk_binary.runtime_bundle_sha256
            == compute_gatk_runtime_bundle_sha256(
                launcher_sha256=result.gatk_binary.executable_sha256,
                local_jar_sha256=result.gatk_binary.local_jar_sha256,
                gatk_version=result.gatk_binary.version,
            )
            # and the execution identity must have USED that bundle
            and ex.gatk_runtime_bundle_sha256 == result.gatk_binary.runtime_bundle_sha256
        ),
        "official_execution_succeeded": ex.job_status == "SUCCEEDED",
        "official_execution_artifacts_published": ex.published_artifact_count == 2,
        "gatk_twin_semantic_parity": par.parity_ok and par.first_difference is None,
        "gatk_only_policy": par.caller == "gatk" and par.subcommand == "HaplotypeCaller",
        "resume_after_restart_verified": (
            res.engines_recreated
            and bool(res.row_counts_before)
            and bool(res.row_counts_after)
            and res.database_fingerprint_before is not None
            and res.artifact_fingerprint_before is not None
        ),
        # every raw observation must agree; an aggregate boolean alone is never enough.
        "resume_creates_no_duplicates": (
            res.duplicate_rows_created == 0
            and bool(res.row_counts_before)
            and res.row_counts_before == res.row_counts_after
            and res.database_fingerprint_before == res.database_fingerprint_after
            and res.artifact_fingerprint_before == res.artifact_fingerprint_after
        ),
        "resume_preserves_terminal_jobs": (
            not res.terminal_job_reset
            and not res.terminal_job_reexecuted
            and not res.artifact_bytes_rewritten
            and res.exact_replay_returned_existing
            and res.database_fingerprint_before == res.database_fingerprint_after
        ),
        "resume_conflicting_replay_rejected": (
            res.conflicting_replay_rejected
            # the conflict experiment must have ACTUALLY run and raised the EXPECTED type
            and res.conflicting_replay_observed
            and res.conflicting_replay_expected_exception is not None
            and res.conflicting_replay_observed_exception
            == res.conflicting_replay_expected_exception
            and res.conflicting_replay_created_rows == 0
            and res.conflicting_replay_db_fingerprint_before is not None
            and res.conflicting_replay_db_fingerprint_before
            == res.conflicting_replay_db_fingerprint_after
            and res.conflicting_replay_artifact_fingerprint_before is not None
            and res.conflicting_replay_artifact_fingerprint_before
            == res.conflicting_replay_artifact_fingerprint_after
        ),
        "resume_exhausted_queue_returns_none": res.exhausted_queue_returns_none,
        "no_stranded_jobs": res.nonterminal_jobs_remaining == 0,
        # an ABSENT failure-control experiment can never satisfy this: the control must have run,
        # produced exactly one bounded failure record, no success row, and stayed FAILED.
        "no_automatic_retry": (
            not res.automatic_retry_observed
            and res.failed_control_observed
            and res.failed_control_job_key is not None
            and res.failed_control_failure_rows == 1
            and res.failed_control_result_rows == 0
            and res.failed_control_retry_executions == 0
            and res.failed_job_remained_failed
            and not res.failed_job_reclaimed
        ),
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
            # the endpoint was read-only BEFORE F7 touched the transaction mode, the role is not
            # write-capable, PostgreSQL itself refused a write, and nothing changed.
            and bnd.operational_read_only_before_set
            and not bnd.operational_role_is_superuser
            and bnd.operational_write_privileges == 0
            and bnd.operational_write_denied_sqlstate == "25006"
            and bnd.operational_fingerprint_before is not None
            and bnd.operational_fingerprint_before == bnd.operational_fingerprint_after
        ),
        "select_config_still_blocked": bnd.select_config_blocked,
        "no_network_access": not bnd.network_access_performed,
    }
    missing = set(HARNESS_READY_REQUIRED_CHECKS) - set(checks)
    if missing:  # pragma: no cover - structural guard
        raise HarnessReadyQualificationError(f"derive_checks omitted required checks: {missing}")
    return checks


def assemble_gate_from_trusted_qualification(
    trusted: Any,
    *,
    created_at: str | None = None,
    evidence: tuple[EvidenceItem, ...] = (),
) -> GateArtifact:
    """Assemble the HARNESS-READY gate from a TRUSTED qualification.

    ``trusted`` must be a :class:`~minos_engine.qualification.l2f_harness_ready_qualifier.
    TrustedQualification`, which only the production qualifier can mint. A caller-constructed
    ``HarnessReadyQualification`` is refused here, so a synthetic observation document can never
    grant HARNESS-READY. The status is still DERIVED: PASS only when every required check derived
    from the observations is true.
    """
    from minos_engine.qualification.l2f_harness_ready_qualifier import TrustedQualification

    if not isinstance(trusted, TrustedQualification):
        raise HarnessReadyQualificationError(
            "HARNESS-READY may only be assembled from a TrustedQualification minted by the "
            "production qualifier; a caller-supplied qualification document is refused"
        )
    result = trusted.result
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
        qualification_tool_version=HARNESS_READY_QUALIFICATION_TOOL_VERSION,
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
    require_qualification_result: bool = True,
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
    # gate integrity proves the bytes were not edited; it says NOTHING about whether the
    # qualifier that produced them still has the current evidence semantics. Bind that here.
    if gate.qualification_tool_version != HARNESS_READY_QUALIFICATION_TOOL_VERSION:
        reasons.append(
            f"gate qualification_tool_version is {gate.qualification_tool_version!r}, expected "
            f"{HARNESS_READY_QUALIFICATION_TOOL_VERSION!r}"
        )
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

    # the canonical qualification result is MANDATORY: a PASS gate is never sufficient on its
    # own, because its embedded mandatory_checks are exactly what an attacker would forge.
    if qualification_path is None and require_qualification_result:
        reasons.append(
            "missing qualification evidence: a HARNESS-READY PASS requires the canonical "
            "qualification result alongside the gate"
        )
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
                # recompute the accepted closure from the SUPPLIED verification root, never
                # from this module's own repository.
                try:
                    recomputed = recompute_accepted_identities(root)
                except MinosEngineError as exc:
                    reasons.append(f"accepted identity recomputation failed at {root}: {exc}")
                    recomputed = None
                if recomputed is not None and parsed.accepted != recomputed:
                    reasons.append(
                        "the qualification result's accepted identities do not equal those "
                        f"recomputed from the verification root {root}"
                    )
                derived = derive_checks(parsed, recomputed_accepted=recomputed, root=root)
                if {k: gate.mandatory_checks.get(k) for k in derived} != derived:
                    reasons.append("gate checks do not equal the checks derived from the result")
                # the committed repository must still carry the accepted identities the result
                # claims, recomputed here from real bytes (offline: no GATK, no database).
                try:
                    verify_accepted_identities(parsed.accepted)
                except MinosEngineError as exc:
                    reasons.append(f"accepted identity closure failed: {exc}")
                if parsed.qualifier_version != HARNESS_READY_QUALIFIER_VERSION:
                    reasons.append(
                        f"qualification result qualifier_version is {parsed.qualifier_version!r}, "
                        f"expected {HARNESS_READY_QUALIFIER_VERSION!r}"
                    )
                if parsed.schema_version != HARNESS_READY_QUALIFIER_SCHEMA:
                    reasons.append(
                        f"qualification result schema_version is {parsed.schema_version!r}, "
                        f"expected {HARNESS_READY_QUALIFIER_SCHEMA!r}"
                    )
                if parsed.source.qualified_source_git_sha != gate.qualified_source_git_sha:
                    reasons.append(
                        "the qualification result and the gate disagree on the qualified source"
                    )

    return {
        "gate_name": HARNESS_READY_GATE,
        "ok": not reasons,
        "gate_hash": gate.gate_hash,
        "status": gate.status.value,
        "reasons": tuple(reasons),
    }
