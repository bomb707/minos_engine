"""Frozen L2-D migration contract (immutable inventory + deterministic hash).

The frozen inventory is the exact object set that revision ``0004_l2d_profile_ingestion``
adds on top of the accepted SPLIT-FROZEN-V2 base (``0003_l2c_split_v2_epochs``) on a clean
PostgreSQL 16 database. Like the earlier stage contracts it is independent of runtime ORM
metadata (the migration is a self-contained snapshot; storage writes use plain SQL), so
the accepted L2-B ``storage_schema_hash`` and every accepted gate are undisturbed.
``l2d_contract_hash`` binds the committed migration bytes to this inventory.
"""

from __future__ import annotations

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "L2D_MIGRATION_REVISION",
    "L2D_DOWN_REVISION",
    "L2D_FROZEN_INVENTORY",
    "l2d_contract_hash",
]

L2D_MIGRATION_REVISION = "0004_l2d_profile_ingestion"
L2D_DOWN_REVISION = "0003_l2c_split_v2_epochs"

L2D_FROZEN_INVENTORY: dict[str, object] = {
    "revision": L2D_MIGRATION_REVISION,
    "down_revision": L2D_DOWN_REVISION,
    "schema_owner": "minos_admin",
    "adds_schemas": [],
    "adds_roles": [],
    "adds_tables": [
        "profiling.bam_profiles",
        "profiling.profile_ingest_attempts",
        "profiling.profile_snapshots",
        "profiling.profile_snapshot_members",
    ],
    "adds_views": [
        "profiling.training_profile_members",
        "evaluation.validation_profile_members",
        "evaluation.sealed_test_profile_members",
    ],
    "adds_triggers": [
        "trg_profiling_bam_profiles_append_only",
        "trg_profiling_profile_ingest_attempts_append_only",
        "trg_profiling_profile_snapshots_append_only",
        "trg_profiling_profile_snapshot_members_append_only",
    ],
    "reused_functions": ["audit.minos_reject_mutation"],
    "reused_tables": [
        "catalog.dataset_registry",
        "catalog.artifacts",
        "catalog.split_snapshots",
    ],
    "m5_admissible_values": ["MATCH", "ABSENT"],
    "artifact_kinds": ["l2d:profile-json", "l2d:profile-manifest-json", "l2d:window-parquet"],
    "partition_values": ["train", "validation", "test"],
    "view_grants": {
        "profiling.training_profile_members": ["minos_trainer"],
        "evaluation.validation_profile_members": ["minos_evaluator"],
        "evaluation.sealed_test_profile_members": [],
    },
    "base_table_app_grants": {
        "profiling.bam_profiles": [],
        "profiling.profile_ingest_attempts": [],
        "profiling.profile_snapshots": [],
        "profiling.profile_snapshot_members": [],
    },
    "legacy_revokes": {
        "profiling.profiles": ["minos_trainer:SELECT", "minos_live:SELECT"],
    },
    "counts": {
        "tables": 4,
        "views": 3,
        "primary_keys": 4,
        "foreign_keys": 9,
        "unique_constraints": 7,
        "check_constraints": 33,
        "indexes": 3,
        "triggers": 4,
    },
}


def l2d_contract_hash(migration_file_sha256: str) -> str:
    """Deterministic contract hash over the committed L2-D migration bytes + inventory."""
    return canonical_hash(
        {
            "migration_file_sha256": migration_file_sha256,
            "frozen_inventory": L2D_FROZEN_INVENTORY,
        }
    )
