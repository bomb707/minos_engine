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
    # SPLIT-FROZEN — Layer 2 stage L2-C immutable dataset registry + 50/10/15 split.
    "SPLIT-FROZEN": frozenset(
        {
            # Accepted prerequisites (unchanged + verified).
            "accepted_protocol_ready_unchanged",
            "accepted_twin_ready_unchanged",
            "accepted_l1_ready_unchanged",
            "accepted_db_ready_unchanged",
            # Exact DB-READY closure ancestry (never current-HEAD substitution).
            "db_ready_source_present",
            "db_ready_source_tree_bound",
            "db_ready_evidence_present",
            "l2c_source_descends_db_ready",
            "head_descends_l2c_source",
            # Canonical manifest + registry bindings (independently recomputed).
            "manifest_schema_valid",
            "manifest_verified",
            "canonical_manifest_hash_bound",
            "dataset_registry_hash_bound",
            "split_policy_hash_bound",
            "parameter_space_hash_bound",
            "feature_registry_hash_bound",
            "committed_manifest_bytes_bound",
            "local_input_inventory_hash_bound",
            # Independent inventory verification + frozen-manifest correspondence.
            "inventory_contract_valid",
            "inventory_hash_recomputed",
            "committed_inventory_bytes_bound",
            "inventory_manifest_identity_bound",
            "inventory_paths_safe",
            "inventory_paths_identity_bound",
            "inventory_truth_mutation_isolation_ok",
            # Generator + manifest-schema evidence cross-bound to unique source evidence.
            "generator_source_evidence_present",
            "generator_source_evidence_matches_source",
            "generator_source_evidence_bound",
            "manifest_schema_evidence_present",
            "manifest_schema_evidence_matches_source",
            "manifest_schema_evidence_bound",
            # Final closure report bytes + non-circular aggregate evidence payload.
            "qualification_report_bytes_bound",
            "evidence_payload_paths_exact",
            "evidence_payload_hash_bound",
            # Immutable L2-C migration bindings (git-tree-bound evidence).
            "l2c_migration_immutable",
            "l2c_migration_file_evidence_bound",
            "l2c_migration_contract_bound",
            "alembic_head_is_l2c",
            # Exact split counts.
            "total_sample_count_75",
            "partition_totals_50_10_15",
            "per_chromosome_10_2_3",
            # Test + static analysis.
            "full_tests_passed",
            "coverage_passed",
            "ruff_passed",
            "format_passed",
            "mypy_passed",
            # Real PostgreSQL 16 integration.
            "postgres_16_verified",
            "l2c_migration_lifecycle_passed",
            "role_isolation_passed",
            "immutability_passed",
            "constraints_passed",
            # Source integrity + leakage + stage gating.
            "evidence_hashes_complete",
            "required_source_tracked",
            "truth_mutation_isolation_ok",
            "service_still_blocked",
        }
    ),
    # SPLIT-FROZEN-V2 — Layer 2 stage L2-C epoched, growth-stable dataset split.
    # Supersedes v1 within L2-C: binds the accepted v1 SPLIT-FROZEN identity and proves
    # the v2 qualified source properly descends the v1 SPLIT-FROZEN evidence commit.
    "SPLIT-FROZEN-V2": frozenset(
        {
            # Accepted prerequisites (unchanged + verified).
            "accepted_protocol_ready_unchanged",
            "accepted_twin_ready_unchanged",
            "accepted_l1_ready_unchanged",
            "accepted_db_ready_unchanged",
            "accepted_split_frozen_unchanged",
            # Exact v1 SPLIT-FROZEN closure ancestry (never current-HEAD substitution).
            "split_frozen_source_present",
            "split_frozen_source_tree_bound",
            "split_frozen_evidence_present",
            "v2_source_descends_split_frozen",
            "head_descends_v2_source",
            # Epoch-1 manifest bindings — EXACT inheritance of the accepted v1 partitions
            # (zero assignment transitions; test/validation cohorts preserved verbatim).
            "epoch_manifest_schema_valid",
            "epoch_manifest_verified",
            "canonical_epoch_manifest_hash_bound",
            "epoch1_inherits_v1_partitions_exactly",
            "epoch1_zero_transitions",
            "epoch1_test_cohort_preserved",
            "epoch1_validation_cohort_preserved",
            "epoch1_parent_fields_null",
            "ancestor_v1_registry_bound",
            "registry_snapshot_hash_bound",
            "split_policy_hash_bound",
            "committed_epoch_manifest_bytes_bound",
            "epoch1_is_first_epoch",
            # Generator + manifest-schema evidence cross-bound to unique source evidence.
            "generator_source_evidence_present",
            "generator_source_evidence_matches_source",
            "generator_source_evidence_bound",
            "manifest_schema_evidence_present",
            "manifest_schema_evidence_matches_source",
            "manifest_schema_evidence_bound",
            # Final closure report bytes + non-circular aggregate evidence payload.
            "qualification_report_bytes_bound",
            "evidence_payload_paths_exact",
            "evidence_payload_hash_bound",
            # Immutable v2 migration bindings (git-tree-bound evidence) + CI head pin.
            "v2_migration_immutable",
            "v2_migration_file_evidence_bound",
            "v2_migration_contract_bound",
            "alembic_head_includes_v2",
            "ci_asserts_head_0003",
            # Exact epoch-1 split counts.
            "total_sample_count_75",
            "partition_totals_50_10_15",
            "per_chromosome_10_2_3",
            # Test + static analysis.
            "full_tests_passed",
            "coverage_passed",
            "ruff_passed",
            "format_passed",
            "mypy_passed",
            # Real PostgreSQL 16 v2 integration (incl. growth immutability + access states).
            "postgres_16_verified",
            "v2_migration_lifecycle_passed",
            "epoch_role_isolation_passed",
            "epoch_immutability_passed",
            "epoch_constraints_passed",
            "epoch_counts_50_10_15_passed",
            "sealed_test_access_denied_passed",
            "validation_evaluator_only_passed",
            "trainer_view_no_features_passed",
            "parent_immutability_passed",
            "growth_new_samples_only_passed",
            "removal_replacement_rejected_passed",
            # Source integrity + leakage + stage gating.
            "evidence_hashes_complete",
            "required_source_tracked",
            "truth_mutation_isolation_ok",
            "service_still_blocked",
        }
    ),
    # INGEST-READY — Layer 2 stage L2-D profile-ingestion CAPABILITY (corpus-independent;
    # per-epoch corpus evidence is the separate PROFILE-SNAPSHOT-FROZEN-<epoch> family).
    "INGEST-READY": frozenset(
        {
            # Accepted prerequisites (unchanged + verified).
            "accepted_protocol_ready_unchanged",
            "accepted_twin_ready_unchanged",
            "accepted_l1_ready_unchanged",
            "accepted_db_ready_unchanged",
            "accepted_split_frozen_unchanged",
            "accepted_split_frozen_v2_unchanged",
            # Exact SPLIT-FROZEN-V2 closure ancestry (never current-HEAD substitution).
            "split_frozen_v2_source_present",
            "split_frozen_v2_source_tree_bound",
            "split_frozen_v2_evidence_present",
            "l2d_source_descends_split_frozen_v2",
            "head_descends_l2d_source",
            # Source evidence cross-binding.
            "ingest_package_evidence_present",
            "ingest_package_evidence_bound",
            "attestation_schema_evidence_present",
            "attestation_schema_evidence_bound",
            "qualification_report_bytes_bound",
            # Immutable single-head L2-D migration bindings.
            "l2d_migration_immutable",
            "l2d_migration_file_evidence_bound",
            "l2d_migration_contract_bound",
            "alembic_single_head_is_l2d",
            "ci_asserts_head_0004",
            # Test + static analysis.
            "full_tests_passed",
            "coverage_passed",
            "ruff_passed",
            "format_passed",
            "mypy_passed",
            # Real PostgreSQL 16 L2-D integration.
            "postgres_16_verified",
            "l2d_migration_lifecycle_passed",
            "ingest_admission_passed",
            "ingest_role_isolation_passed",
            "sealed_test_profile_access_denied",
            "legacy_profiles_reads_revoked",
            "profile_snapshot_freeze_passed",
            "three_artifact_contract_passed",
            "exact_byte_hashing_passed",
            "artifact_conflict_passed",
            "idempotency_passed",
            "content_conflict_passed",
            "profile_id_conflict_passed",
            "concurrent_serialization_passed",
            "epoch_membership_passed",
            "parquet_invariants_passed",
            "fasta_bounds_passed",
            "version_selection_passed",
            "atomic_audit_passed",
            "trainer_view_isolation_passed",
            # Source integrity + stage gating.
            "evidence_hashes_complete",
            "required_source_tracked",
            "service_still_blocked",
        }
    ),
}


def required_checks_for(gate_name: str) -> frozenset[str]:
    """Return the required-check set for a gate name (empty if unregistered)."""
    return REQUIRED_CHECKS.get(gate_name, frozenset())
