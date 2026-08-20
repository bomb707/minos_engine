"""Frozen contract for the L2-F F3-A migration ``0006_l2f_experiment_plan``.

Binds the additive experiment-plan migration's identity: revision lineage, the exact
five canonical tables, the additive composite-UNIQUE targets on the immutable 0004/0005
tables, the declarative composite foreign keys that enforce the train lineage, the
immutability triggers, and the byte SHA-256 of every accepted prior migration (0001-0005)
so a change to any of them is detected. A deterministic ``l2f_contract_hash`` is computed
over the migration file bytes + this inventory, mirroring the L2-B/L2-C/L2-D contracts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "L2F_MIGRATION_REVISION",
    "L2F_DOWN_REVISION",
    "L2F_MIGRATION_FILE",
    "L2F_TABLES",
    "L2F_COMPOSITE_TARGETS",
    "L2F_COMPOSITE_FKS",
    "L2F_TRIGGERS",
    "ACCEPTED_PRIOR_MIGRATION_SHAS",
    "L2F_INVENTORY",
    "l2f_contract_hash",
]

L2F_MIGRATION_REVISION = "0006_l2f_experiment_plan"
L2F_DOWN_REVISION = "0005_l2e_feature_view"
L2F_MIGRATION_FILE = "migrations/versions/0006_l2f_experiment_plan.py"

#: The five canonical L2-F tables (experiments schema). Legacy jobs/results/profiles
#: are NOT touched and are not part of this set.
L2F_TABLES = (
    "l2f_experiment_plans",
    "l2f_experiment_plan_members",
    "l2f_config_payloads",
    "l2f_experiment_plan_configs",
    "l2f_experiment_jobs",
)

#: Additive composite-UNIQUE targets added to immutable 0004/0005 tables (removed on
#: downgrade) — the composite-FK targets that make the train lineage declarative.
L2F_COMPOSITE_TARGETS = (
    ("profiling", "feature_matrices", "uq_l2f_feature_matrices_composite"),
    ("profiling", "profile_snapshot_members", "uq_l2f_psm_composite"),
    ("profiling", "feature_matrix_members", "uq_l2f_fmm_composite"),
    ("catalog", "artifacts", "uq_l2f_artifacts_id_sha256"),
)

#: Declarative composite foreign keys enforcing every cross-table invariant.
L2F_COMPOSITE_FKS = (
    "fk_l2f_plans_train_matrix_lineage",
    "fk_l2f_pm_plan_lineage",
    "fk_l2f_pm_snapshot_member",
    "fk_l2f_pm_matrix_member",
    "fk_l2f_cp_artifact_sha",
    "fk_l2f_pc_payload_hash",
    "fk_l2f_job_member_plan",
    "fk_l2f_job_config_plan",
)

#: Immutability triggers (reused append-only fn + new job identity-change fn).
L2F_TRIGGERS = (
    "trg_experiments_l2f_experiment_plans_append_only",
    "trg_experiments_l2f_experiment_plan_members_append_only",
    "trg_experiments_l2f_config_payloads_append_only",
    "trg_experiments_l2f_experiment_plan_configs_append_only",
    "trg_l2f_jobs_identity_immutable",
    "trg_l2f_jobs_no_delete",
)

#: Byte SHA-256 of every accepted prior migration — proves 0001-0005 are byte-identical.
ACCEPTED_PRIOR_MIGRATION_SHAS: dict[str, str] = {
    "migrations/versions/0001_l2b_initial.py": "7cb904702a3d7e6861c3f828590fff59cff04dc42b90a76b2a617c2c77f03f12",
    "migrations/versions/0002_l2c_dataset_split.py": "f3dd195311959ce8caf079b7d9ceb00731bd72a4e7c22485338778885a0a96c6",
    "migrations/versions/0003_l2c_split_v2_epochs.py": "16446b8cfd82900180a6f7a04a62f35fc463597857265caa25376f38e7c66f9c",
    "migrations/versions/0004_l2d_profile_ingestion.py": "f540ba7f5c88ada0e1da9948f5bc7ae97d37dc3cbb1394b192c8c498e739ae0e",
    "migrations/versions/0005_l2e_feature_view.py": "21254bbeb6f131d043532127b1baaee51d2a50c8c2049967cd7007ba5aeefb23",
}

L2F_INVENTORY: dict[str, object] = {
    "revision": L2F_MIGRATION_REVISION,
    "down_revision": L2F_DOWN_REVISION,
    "tables": list(L2F_TABLES),
    "composite_targets": [list(t) for t in L2F_COMPOSITE_TARGETS],
    "composite_fks": list(L2F_COMPOSITE_FKS),
    "triggers": list(L2F_TRIGGERS),
    "prior_migration_shas": ACCEPTED_PRIOR_MIGRATION_SHAS,
}


def l2f_contract_hash(migration_file_sha256: str) -> str:
    """Deterministic contract hash over the migration file bytes + frozen inventory."""
    return canonical_hash(
        {"migration_file_sha256": migration_file_sha256, "inventory": L2F_INVENTORY}
    )


def migration_file_sha256() -> str:
    return hashlib.sha256(Path(L2F_MIGRATION_FILE).read_bytes()).hexdigest()
