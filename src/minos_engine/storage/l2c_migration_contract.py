"""Frozen L2-C migration contract (immutable inventory + deterministic hash).

The frozen inventory is the exact object set that revision ``0002_l2c_dataset_split``
adds on top of the L2-B base (``0001_l2b_initial``) on a clean PostgreSQL 16 database.
It is independent of runtime ORM metadata — the L2-C dataset-split tables are defined
in a dedicated Core ``MetaData`` that is intentionally NOT part of the L2-B
``Base.metadata``, so the L2-B ``storage_schema_hash`` (and the accepted DB-READY gate
that binds it) is never disturbed. ``l2c_contract_hash`` binds the committed L2-C
migration bytes to this inventory.
"""

from __future__ import annotations

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "L2C_MIGRATION_REVISION",
    "L2C_DOWN_REVISION",
    "L2C_FROZEN_INVENTORY",
    "l2c_contract_hash",
]

L2C_MIGRATION_REVISION = "0002_l2c_dataset_split"
L2C_DOWN_REVISION = "0001_l2b_initial"

L2C_FROZEN_INVENTORY: dict[str, object] = {
    "revision": L2C_MIGRATION_REVISION,
    "down_revision": L2C_DOWN_REVISION,
    "schema_owner": "minos_admin",
    "adds_schemas": [],
    "adds_roles": [],
    "adds_tables": [
        "catalog.dataset_registry",
        "catalog.split_allocations",
        "evaluation.dataset_evaluation_identity",
    ],
    "adds_views": [
        "catalog.training_dataset_allocations",
        "evaluation.locked_allocations",
    ],
    "adds_triggers": [
        "trg_catalog_dataset_registry_append_only",
        "trg_catalog_split_allocations_append_only",
        "trg_evaluation_dataset_evaluation_identity_append_only",
    ],
    "reused_functions": ["audit.minos_reject_mutation"],
    "partition_values": ["train", "validation", "test"],
    "view_grants": {
        "catalog.training_dataset_allocations": ["minos_trainer"],
        "evaluation.locked_allocations": ["minos_evaluator"],
    },
    "base_table_app_grants": {
        "catalog.dataset_registry": [],
        "catalog.split_allocations": [],
        "evaluation.dataset_evaluation_identity": ["minos_evaluator:SELECT,INSERT"],
    },
    "counts": {
        "tables": 3,
        "views": 2,
        "primary_keys": 3,
        "foreign_keys": 2,
        "unique_constraints": 6,
        "check_constraints": 24,
        "indexes": 2,
        "triggers": 3,
    },
}


def l2c_contract_hash(migration_file_sha256: str) -> str:
    """Deterministic contract hash over the committed L2-C migration bytes + inventory."""
    return canonical_hash(
        {
            "migration_file_sha256": migration_file_sha256,
            "frozen_inventory": L2C_FROZEN_INVENTORY,
        }
    )
