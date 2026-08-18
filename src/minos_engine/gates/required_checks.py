"""Versioned required-check registry, keyed by gate name.

A PASS gate for a known gate name must contain *every* required check and each
required value must be true. Unknown (supplemental) checks may be retained but
may never be false in a PASS gate. This prevents a PASS gate from being built
from an arbitrary ``{"anything": true}`` dictionary.

This module holds data only (no imports from the gate contract) to avoid an
import cycle: ``gates.contracts`` imports this, not the reverse.
"""

from __future__ import annotations

__all__ = ["REQUIRED_CHECKS", "required_checks_for"]

REQUIRED_CHECKS: dict[str, frozenset[str]] = {
    "PROTOCOL-READY": frozenset(
        {
            "all_tests_pass",
            "tests_collected_nonzero",
            "ruff_check_pass",
            "ruff_format_pass",
            "mypy_pass",
            "canonical_determinism",
            "gatk_only_policy",
            "gatk_registry_has_25_params",
            "required_identity_fails_closed",
            "layer1_not_implemented",
            "layer2_blocked",
            "no_truth_or_locked_test_access",
            "six_schemas_present",
            "documentation_complete",
            "ci_workflow_present",
            "evidence_hashes_complete",
            "qualified_source_clean",
            "coverage_threshold_met",
            "specifications_present",
            # Git-tree-bound source integrity (no untracked/ignored evidence).
            "required_source_tracked",
            "worktree_matches_head",
        }
    ),
    "TWIN-READY": frozenset(
        {
            # Stage 0 prerequisite (accepted identity + evidence + promotion)
            "protocol_ready_identity_accepted",
            "protocol_ready_evidence_verified",
            "protocol_ready_promotion_authorized",
            # Twin behavior
            "twin_contracts_schema_valid",
            "twin_canonical_hashing_deterministic",
            "gatk_execution_plan_ok",
            "gatk_only_policy",
            "comparison_parser_ok",
            "scoring_matches_declared_level",
            "unavailable_scoring_honest",
            "fixture_replay_deterministic",
            "parity_mismatch_detection_works",
            "truth_isolation_ok",
            "architecture_boundaries_ok",
            "no_hidden_network_dependency",
            # Test + static analysis
            "tests_collected_nonzero",
            "all_tests_pass",
            "ruff_check_pass",
            "ruff_format_pass",
            "mypy_pass",
            "coverage_threshold_met",
            "python_runtime_is_3_12",
            # Git-tree-bound source integrity
            "required_source_tracked",
            "worktree_matches_head",
            "evidence_hashes_complete",
            "qualified_source_clean",
            "qualified_source_present",
            # Documentation + stage gating
            "stage1_documentation_complete",
            "layer1_not_implemented",
            "layer2_blocked",
        }
    ),
    # L1-READY — complete fail-closed set for the Layer 1 qualification gate.
    # Layer 2 remains blocked until a PASS L1-READY verifies through the entry gate.
    "L1-READY": frozenset(
        {
            # Accepted prerequisites (Stage 0 + Stage 1 identities, git-bound).
            "protocol_ready_identity_accepted",
            "protocol_ready_evidence_verified",
            "twin_ready_identity_accepted",
            "twin_ready_evidence_verified",
            "twin_ready_promotion_authorized",
            "python_runtime_is_3_12",
            # Layer 1 behavior.
            "layer1_contracts_schema_valid",
            "layer1_input_validation_complete",
            "layer1_filter_policy_verified",
            "layer1_feature_known_answers_pass",
            "layer1_reference_profiler_verified",
            "layer1_determinism_verified",
            "layer1_truth_isolation_verified",
            "layer1_architecture_boundaries_verified",
            "layer1_deadline_behavior_verified",
            "layer1_hard_limit_met",
            "layer1_memory_policy_verified",
            "layer1_real_bam_qualified",
            # Identity binding.
            "profile_schema_hash_match",
            "profiler_config_hash_match",
            "profiler_version_match",
            # Git-tree-bound source integrity.
            "required_source_tracked",
            "worktree_matches_head",
            "evidence_hashes_complete",
            "qualified_source_clean",
            "qualified_source_present",
            # Test + static analysis.
            "tests_collected_nonzero",
            "all_tests_pass",
            "coverage_threshold_met",
            "ruff_check_pass",
            "ruff_format_pass",
            "mypy_pass",
            # Documentation + stage gating.
            "layer1_documentation_complete",
            "layer2_blocked",
        }
    ),
    # DB-READY — Layer 2 stage L2-B PostgreSQL storage foundation.
    "DB-READY": frozenset(
        {
            "l2a_entry_passed",
            "postgres_16_verified",
            "seven_schemas_created",
            "five_roles_created",
            "migration_upgrade_passed",
            "migration_downgrade_passed",
            "migration_reupgrade_passed",
            "constraint_tests_passed",
            "foreign_keys_passed",
            "append_only_passed",
            "least_privilege_passed",
            "live_evaluation_denied",
            "trainer_evaluation_denied",
            "worker_claim_concurrency_passed",
            "artifact_policy_passed",
            "service_still_blocked",
            "split_manifest_absent",
            "full_tests_passed",
            "coverage_passed",
            "ruff_passed",
            "format_passed",
            "mypy_passed",
            "accepted_prerequisites_unchanged",
            # Immutable migration + admin ownership + final L2-A closure binding.
            "migration_immutable",
            "migration_contract_frozen",
            "migration_file_evidence_bound",
            "migration_contract_bound",
            "admin_ownership_passed",
            "l2a_closure_source_bound",
            "l2a_closure_source_tree_bound",
            "l2a_closure_evidence_bound",
            "l2a_closure_evidence_tree_bound",
            "l2a_closure_chain_valid",
            # Exact qualified-source ancestry (never current-HEAD substitution).
            "l2b_qualified_source_present",
            "l2b_qualified_source_tree_bound",
            "l2b_qualified_source_descends_l2a",
            "head_descends_l2b_qualified_source",
            "accepted_feature_registry_hash_bound",
        }
    ),
}


def required_checks_for(gate_name: str) -> frozenset[str]:
    """Return the required-check set for a gate name (empty if unregistered)."""
    return REQUIRED_CHECKS.get(gate_name, frozenset())
