"""Frozen L2-B migration contract (immutable inventory + deterministic hash).

The frozen inventory is the exact object set that revision ``0001_l2b_initial`` must
produce on a clean PostgreSQL 16 database. It is independent of runtime ORM metadata
(that is the whole point of the immutable migration), so later ORM changes cannot
alter it. ``contract_hash`` binds the committed migration bytes to this inventory.
"""

from __future__ import annotations

from minos_engine.common.hashing import canonical_hash

__all__ = ["MIGRATION_REVISION", "FROZEN_INVENTORY", "contract_hash"]

MIGRATION_REVISION = "0001_l2b_initial"

FROZEN_INVENTORY: dict[str, object] = {
    "revision": MIGRATION_REVISION,
    "schema_owner": "minos_admin",
    "schemas": [
        "catalog",
        "profiling",
        "experiments",
        "evaluation",
        "models",
        "runtime",
        "audit",
    ],
    "roles": [
        "minos_live",
        "minos_runner",
        "minos_evaluator",
        "minos_trainer",
        "minos_admin",
    ],
    "tables": [
        "catalog.artifacts",
        "catalog.gatk_configs",
        "catalog.datasets",
        "profiling.profiles",
        "experiments.jobs",
        "experiments.results",
        "evaluation.evaluations",
        "models.model_bundles",
        "runtime.decisions",
        "audit.events",
    ],
    "functions": [
        "audit.minos_reject_mutation",
        "experiments.minos_reject_identity_change",
    ],
    "triggers": [
        "trg_profiling_profiles_append_only",
        "trg_experiments_results_append_only",
        "trg_evaluation_evaluations_append_only",
        "trg_models_model_bundles_append_only",
        "trg_runtime_decisions_append_only",
        "trg_audit_events_append_only",
        "trg_experiments_jobs_identity_immutable",
    ],
    "counts": {
        "schemas": 7,
        "roles": 5,
        "tables": 10,
        "primary_keys": 10,
        "foreign_keys": 9,
        "unique_constraints": 12,
        "check_constraints": 29,
        "indexes": 23,
        "functions": 2,
        "triggers": 7,
    },
}


def contract_hash(migration_file_sha256: str) -> str:
    """Deterministic contract hash over the committed migration bytes + inventory."""
    return canonical_hash(
        {"migration_file_sha256": migration_file_sha256, "frozen_inventory": FROZEN_INVENTORY}
    )
