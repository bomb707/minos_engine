"""Frozen L2-C SPLIT-FROZEN-v2 migration contract (immutable inventory + hash).

The frozen inventory is the exact object set that revision ``0003_l2c_split_v2_epochs``
adds on top of the L2-C base (``0002_l2c_dataset_split``) on a clean PostgreSQL 16
database. Like the v1 L2-C contract it is independent of runtime ORM metadata — the v2
epoch tables are defined only in the migration (no ``Base.metadata``), so the accepted
L2-B ``storage_schema_hash`` and the accepted DB-READY / SPLIT-FROZEN gates are never
disturbed. ``l2c_split_v2_contract_hash`` binds the committed migration bytes to this
inventory.
"""

from __future__ import annotations

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "L2C_SPLIT_V2_MIGRATION_REVISION",
    "L2C_SPLIT_V2_DOWN_REVISION",
    "L2C_SPLIT_V2_FROZEN_INVENTORY",
    "l2c_split_v2_contract_hash",
]

L2C_SPLIT_V2_MIGRATION_REVISION = "0003_l2c_split_v2_epochs"
L2C_SPLIT_V2_DOWN_REVISION = "0002_l2c_dataset_split"

L2C_SPLIT_V2_FROZEN_INVENTORY: dict[str, object] = {
    "revision": L2C_SPLIT_V2_MIGRATION_REVISION,
    "down_revision": L2C_SPLIT_V2_DOWN_REVISION,
    "schema_owner": "minos_admin",
    "adds_schemas": [],
    "adds_roles": [],
    "adds_tables": [
        "catalog.split_snapshots",
        "catalog.split_epoch_allocations",
    ],
    "adds_views": [
        "catalog.training_epoch_allocations",
        "evaluation.validation_epoch_allocations",
        "evaluation.sealed_test_epoch_allocations",
    ],
    "adds_triggers": [
        "trg_catalog_split_snapshots_append_only",
        "trg_catalog_split_epoch_allocations_append_only",
    ],
    "reused_functions": ["audit.minos_reject_mutation"],
    "reused_tables": ["catalog.dataset_registry"],
    "partition_values": ["train", "validation", "test"],
    "assignment_sources": ["v1-inherited", "v2-policy"],
    "view_grants": {
        "catalog.training_epoch_allocations": ["minos_trainer"],
        "evaluation.validation_epoch_allocations": ["minos_evaluator"],
        "evaluation.sealed_test_epoch_allocations": [],
    },
    "base_table_app_grants": {
        "catalog.split_snapshots": [],
        "catalog.split_epoch_allocations": [],
    },
    "counts": {
        "tables": 2,
        "views": 3,
        "primary_keys": 2,
        "foreign_keys": 3,
        "unique_constraints": 4,
        "check_constraints": 15,
        "indexes": 2,
        "triggers": 2,
    },
}


def l2c_split_v2_contract_hash(migration_file_sha256: str) -> str:
    """Deterministic contract hash over the committed v2 migration bytes + inventory."""
    return canonical_hash(
        {
            "migration_file_sha256": migration_file_sha256,
            "frozen_inventory": L2C_SPLIT_V2_FROZEN_INVENTORY,
        }
    )
