"""Frozen contract for the L2-F F3-A migration ``0006_l2f_experiment_plan``.

Binds the additive experiment-plan migration's identity: revision lineage, the exact five
canonical tables and six external mapping stubs, the additive composite-UNIQUE targets on the
immutable 0004/0005 tables, the declarative composite foreign keys, the immutability triggers,
the L2-F job identity function, and the byte SHA-256 of every accepted prior migration.

F3-A closure adds an **exhaustive frozen static inventory (pending acceptance)**
(``L2F_STATIC_INVENTORY``) of the deployed 0006 schema — every owned table's ordered
columns/types/nullability/defaults, all constraints with full normalized definitions and
options, all indexes, the six composite targets, the six triggers, the job function, ownership,
raw + **effective** ACLs (via ``aclexplode`` over ``COALESCE(acl, acldefault(...))``), and the
absence of effective application-role/PUBLIC table grants. The inventory was captured once from
a live scratch upgrade using the read-only introspector at
``tests/integration/layer2_db/l2f_introspect.py``; tests re-derive the live inventory and assert
exact equality. Until this corrective commit is reviewed and explicitly accepted, treat this as
"frozen static inventory pending acceptance", not owner-reviewed.

``L2F_MIGRATION_SHA256`` freezes the migration file bytes and ``L2F_CONTRACT_HASH`` is a
domain-separated canonical hash over the migration SHA + revision lineage + accepted prior
migration hashes + the full static inventory (no self-reference in its preimage).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "L2F_MIGRATION_REVISION",
    "L2F_DOWN_REVISION",
    "L2F_MIGRATION_FILE",
    "L2F_TABLES",
    "L2F_EXTERNAL_STUB_TABLES",
    "L2F_JOB_FUNCTION",
    "L2F_COMPOSITE_TARGETS",
    "L2F_COMPOSITE_FKS",
    "L2F_PLAN_LOGICAL_IDENTITY",
    "L2F_CONFIG_PAYLOAD_SCHEMA",
    "L2F_CONFIG_PAYLOAD_MEDIA_TYPE",
    "L2F_TRIGGERS",
    "ACCEPTED_PRIOR_MIGRATION_SHAS",
    "L2F_INVENTORY",
    "L2F_LIVE_INVENTORY",
    "L2F_STATIC_INVENTORY",
    "L2F_MIGRATION_SHA256",
    "L2F_CONTRACT_HASH",
    "CONTRACT_DOMAIN",
    "CONTRACT_VERSION",
    "l2f_contract_hash",
    "migration_file_sha256",
    "compute_migration_sha256",
    "contract_preimage",
    "compute_contract_hash",
]

L2F_MIGRATION_REVISION = "0006_l2f_experiment_plan"
L2F_DOWN_REVISION = "0005_l2e_feature_view"
L2F_MIGRATION_FILE = "migrations/versions/0006_l2f_experiment_plan.py"

#: The five canonical L2-F tables (experiments schema).
L2F_TABLES = (
    "l2f_experiment_plans",
    "l2f_experiment_plan_members",
    "l2f_config_payloads",
    "l2f_experiment_plan_configs",
    "l2f_experiment_jobs",
)

#: The six external upstream tables the private Core mappings declare as FK-resolution stubs.
L2F_EXTERNAL_STUB_TABLES = (
    "profiling.profile_snapshots",
    "profiling.feature_sets",
    "profiling.feature_matrices",
    "profiling.profile_snapshot_members",
    "profiling.feature_matrix_members",
    "catalog.artifacts",
)

#: The L2-F job scientific-identity immutability function.
L2F_JOB_FUNCTION = "experiments.minos_l2f_reject_job_identity_change"

#: Additive composite-UNIQUE targets added to immutable 0004/0005 tables (removed on downgrade).
L2F_COMPOSITE_TARGETS = (
    ("profiling", "feature_matrices", "uq_l2f_feature_matrices_composite"),
    ("profiling", "profile_snapshots", "uq_l2f_profile_snapshots_composite"),
    ("profiling", "feature_sets", "uq_l2f_feature_sets_composite"),
    ("profiling", "profile_snapshot_members", "uq_l2f_psm_composite"),
    ("profiling", "feature_matrix_members", "uq_l2f_fmm_composite"),
    ("catalog", "artifacts", "uq_l2f_artifacts_id_sha_media"),
)

#: Declarative composite foreign keys enforcing every cross-table invariant.
L2F_COMPOSITE_FKS = (
    "fk_l2f_plans_snapshot_identity",
    "fk_l2f_plans_feature_set_identity",
    "fk_l2f_plans_train_matrix_lineage",
    "fk_l2f_pm_plan_lineage",
    "fk_l2f_pm_snapshot_member",
    "fk_l2f_pm_matrix_member",
    "fk_l2f_cp_artifact_sha_media",
    "fk_l2f_pc_plan_param_space",
    "fk_l2f_pc_payload_identity",
    "fk_l2f_job_member_plan",
    "fk_l2f_job_config_plan",
)

#: The complete logical-plan identity columns (counts derived + verified downstream).
L2F_PLAN_LOGICAL_IDENTITY = (
    "snapshot_hash",
    "split_manifest_hash",
    "registry_snapshot_hash",
    "train_matrix_hash",
    "train_feature_view_hash",
    "feature_set_hash",
    "feature_registry_hash",
    "gatk_registry_hash",
    "parameter_space_hash",
    "experiment_parameter_policy_hash",
    "candidate_set_hash",
)

#: Frozen CONFIG-payload artifact identity.
L2F_CONFIG_PAYLOAD_SCHEMA = "l2f-config-payload-v1"
L2F_CONFIG_PAYLOAD_MEDIA_TYPE = "application/vnd.minos.l2f-config+json"

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

#: Compact legacy inventory (retained; superseded by L2F_STATIC_INVENTORY for hashing).
L2F_INVENTORY: dict[str, object] = {
    "revision": L2F_MIGRATION_REVISION,
    "down_revision": L2F_DOWN_REVISION,
    "tables": list(L2F_TABLES),
    "composite_targets": [list(t) for t in L2F_COMPOSITE_TARGETS],
    "composite_fks": list(L2F_COMPOSITE_FKS),
    "plan_logical_identity": list(L2F_PLAN_LOGICAL_IDENTITY),
    "config_payload_schema": L2F_CONFIG_PAYLOAD_SCHEMA,
    "config_payload_media_type": L2F_CONFIG_PAYLOAD_MEDIA_TYPE,
    "triggers": list(L2F_TRIGGERS),
    "prior_migration_shas": ACCEPTED_PRIOR_MIGRATION_SHAS,
}

# --------------------------------------------------------------------------- #
# Exhaustive frozen static inventory (pending acceptance), captured once from a live
# scratch 0006 upgrade via tests/integration/layer2_db/l2f_introspect.py with raw + effective
# ACLs. Tests re-derive the live inventory and assert exact equality against this literal.
# --------------------------------------------------------------------------- #
_L2F_LIVE_INVENTORY_JSON = """{
 "composite_targets": [
  {
   "columns": [
    "id",
    "sha256",
    "media_type"
   ],
   "definition": "UNIQUE (id, sha256, media_type)",
   "name": "uq_l2f_artifacts_id_sha_media",
   "schema": "catalog",
   "table": "artifacts",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "id",
    "profile_snapshot_id",
    "partition",
    "matrix_hash",
    "feature_set_id"
   ],
   "definition": "UNIQUE (id, profile_snapshot_id, partition, matrix_hash, feature_set_id)",
   "name": "uq_l2f_feature_matrices_composite",
   "schema": "profiling",
   "table": "feature_matrices",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "id",
    "feature_matrix_id",
    "dataset_registry_id",
    "member_index",
    "feature_values_hash"
   ],
   "definition": "UNIQUE (id, feature_matrix_id, dataset_registry_id, member_index, feature_values_hash)",
   "name": "uq_l2f_fmm_composite",
   "schema": "profiling",
   "table": "feature_matrix_members",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "id",
    "feature_set_hash",
    "registry_hash"
   ],
   "definition": "UNIQUE (id, feature_set_hash, registry_hash)",
   "name": "uq_l2f_feature_sets_composite",
   "schema": "profiling",
   "table": "feature_sets",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "id",
    "profile_snapshot_id",
    "dataset_registry_id",
    "bam_profile_id",
    "partition",
    "feature_values_hash"
   ],
   "definition": "UNIQUE (id, profile_snapshot_id, dataset_registry_id, bam_profile_id, partition, feature_values_hash)",
   "name": "uq_l2f_psm_composite",
   "schema": "profiling",
   "table": "profile_snapshot_members",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "id",
    "snapshot_hash",
    "split_manifest_hash",
    "registry_snapshot_hash"
   ],
   "definition": "UNIQUE (id, snapshot_hash, split_manifest_hash, registry_snapshot_hash)",
   "name": "uq_l2f_profile_snapshots_composite",
   "schema": "profiling",
   "table": "profile_snapshots",
   "type": "UNIQUE"
  }
 ],
 "experiments_schema": [
  {
   "acl_effective": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "CREATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "USAGE"
    },
    {
     "grantable": false,
     "grantee": "minos_evaluator",
     "grantor": "minos_admin",
     "privilege": "USAGE"
    },
    {
     "grantable": false,
     "grantee": "minos_runner",
     "grantor": "minos_admin",
     "privilege": "USAGE"
    },
    {
     "grantable": false,
     "grantee": "minos_trainer",
     "grantor": "minos_admin",
     "privilege": "USAGE"
    }
   ],
   "acl_is_default": false,
   "acl_raw": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "CREATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "USAGE"
    },
    {
     "grantable": false,
     "grantee": "minos_evaluator",
     "grantor": "minos_admin",
     "privilege": "USAGE"
    },
    {
     "grantable": false,
     "grantee": "minos_runner",
     "grantor": "minos_admin",
     "privilege": "USAGE"
    },
    {
     "grantable": false,
     "grantee": "minos_trainer",
     "grantor": "minos_admin",
     "privilege": "USAGE"
    }
   ],
   "owner": "minos_admin",
   "schema": "experiments"
  }
 ],
 "job_function": [
  {
   "acl_effective": [
    {
     "grantable": false,
     "grantee": "PUBLIC",
     "grantor": "minos_admin",
     "privilege": "EXECUTE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "EXECUTE"
    }
   ],
   "acl_is_default": true,
   "acl_raw": [],
   "body_md5": "7315057dc52f51d62e0b08b51aa1ac25",
   "config": [
    "search_path=pg_catalog"
   ],
   "identity_arguments": "",
   "language": "plpgsql",
   "name": "minos_l2f_reject_job_identity_change",
   "owner": "minos_admin",
   "parallel": "unsafe",
   "result_type": "trigger",
   "schema": "experiments",
   "security_definer": false,
   "strict": false,
   "volatility": "volatile"
  }
 ],
 "no_app_role_grants": true,
 "owned_constraints": [
  {
   "columns": [
    "config_hash"
   ],
   "definition": "CHECK (config_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_cp_config_hash_hex",
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "type": "CHECK"
  },
  {
   "columns": [
    "media_type"
   ],
   "definition": "CHECK (media_type = 'application/vnd.minos.l2f-config+json'::text)",
   "name": "ck_l2f_cp_media_type",
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "type": "CHECK"
  },
  {
   "columns": [
    "parameter_space_hash"
   ],
   "definition": "CHECK (parameter_space_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_cp_param_space_hex",
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "type": "CHECK"
  },
  {
   "columns": [
    "schema_version"
   ],
   "definition": "CHECK (schema_version = 'l2f-config-payload-v1'::text)",
   "name": "ck_l2f_cp_schema_version",
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "type": "CHECK"
  },
  {
   "columns": [
    "artifact_id",
    "config_hash",
    "media_type"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (artifact_id, config_hash, media_type) REFERENCES catalog.artifacts(id, sha256, media_type)",
   "match": "SIMPLE",
   "name": "fk_l2f_cp_artifact_sha_media",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "sha256",
    "media_type"
   ],
   "referred_schema": "catalog",
   "referred_table": "artifacts",
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "id"
   ],
   "definition": "PRIMARY KEY (id)",
   "name": "pk_l2f_config_payloads",
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "type": "PRIMARY KEY"
  },
  {
   "columns": [
    "config_hash"
   ],
   "definition": "UNIQUE (config_hash)",
   "name": "uq_l2f_config_payloads_config_hash",
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "id",
    "config_hash",
    "parameter_space_hash"
   ],
   "definition": "UNIQUE (id, config_hash, parameter_space_hash)",
   "name": "uq_l2f_config_payloads_id_hash_ps",
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "job_key"
   ],
   "definition": "CHECK (job_key ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_jobs_job_key_hex",
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "type": "CHECK"
  },
  {
   "columns": [
    "status"
   ],
   "definition": "CHECK (status = ANY (ARRAY['PENDING'::text, 'CLAIMED'::text, 'RUNNING'::text, 'SUCCEEDED'::text, 'FAILED'::text, 'CANCELLED'::text]))",
   "name": "ck_l2f_jobs_status_valid",
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "type": "CHECK"
  },
  {
   "columns": [
    "plan_config_id",
    "plan_id"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (plan_config_id, plan_id) REFERENCES experiments.l2f_experiment_plan_configs(id, plan_id)",
   "match": "SIMPLE",
   "name": "fk_l2f_job_config_plan",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "plan_id"
   ],
   "referred_schema": "experiments",
   "referred_table": "l2f_experiment_plan_configs",
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "plan_member_id",
    "plan_id"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (plan_member_id, plan_id) REFERENCES experiments.l2f_experiment_plan_members(id, plan_id)",
   "match": "SIMPLE",
   "name": "fk_l2f_job_member_plan",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "plan_id"
   ],
   "referred_schema": "experiments",
   "referred_table": "l2f_experiment_plan_members",
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "plan_id"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (plan_id) REFERENCES experiments.l2f_experiment_plans(id)",
   "match": "SIMPLE",
   "name": "fk_l2f_job_plan_id",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id"
   ],
   "referred_schema": "experiments",
   "referred_table": "l2f_experiment_plans",
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "id"
   ],
   "definition": "PRIMARY KEY (id)",
   "name": "pk_l2f_experiment_jobs",
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "type": "PRIMARY KEY"
  },
  {
   "columns": [
    "job_key"
   ],
   "definition": "UNIQUE (job_key)",
   "name": "uq_l2f_jobs_job_key",
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "plan_id",
    "plan_member_id",
    "plan_config_id"
   ],
   "definition": "UNIQUE (plan_id, plan_member_id, plan_config_id)",
   "name": "uq_l2f_jobs_logical_identity",
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "config_hash"
   ],
   "definition": "CHECK (config_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_pc_config_hash_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "CHECK"
  },
  {
   "columns": [
    "config_index"
   ],
   "definition": "CHECK (config_index >= 0)",
   "name": "ck_l2f_pc_config_index_nonneg",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "CHECK"
  },
  {
   "columns": [
    "parameter_space_hash"
   ],
   "definition": "CHECK (parameter_space_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_pc_param_space_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "CHECK"
  },
  {
   "columns": [
    "config_payload_id",
    "config_hash",
    "parameter_space_hash"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (config_payload_id, config_hash, parameter_space_hash) REFERENCES experiments.l2f_config_payloads(id, config_hash, parameter_space_hash)",
   "match": "SIMPLE",
   "name": "fk_l2f_pc_payload_identity",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "config_hash",
    "parameter_space_hash"
   ],
   "referred_schema": "experiments",
   "referred_table": "l2f_config_payloads",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "plan_id",
    "parameter_space_hash"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (plan_id, parameter_space_hash) REFERENCES experiments.l2f_experiment_plans(id, parameter_space_hash)",
   "match": "SIMPLE",
   "name": "fk_l2f_pc_plan_param_space",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "parameter_space_hash"
   ],
   "referred_schema": "experiments",
   "referred_table": "l2f_experiment_plans",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "id"
   ],
   "definition": "PRIMARY KEY (id)",
   "name": "pk_l2f_experiment_plan_configs",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "PRIMARY KEY"
  },
  {
   "columns": [
    "id",
    "plan_id"
   ],
   "definition": "UNIQUE (id, plan_id)",
   "name": "uq_l2f_pc_id_plan",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "plan_id",
    "config_index"
   ],
   "definition": "UNIQUE (plan_id, config_index)",
   "name": "uq_l2f_pc_plan_index",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "plan_id",
    "config_payload_id"
   ],
   "definition": "UNIQUE (plan_id, config_payload_id)",
   "name": "uq_l2f_pc_plan_payload",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "feature_values_hash"
   ],
   "definition": "CHECK (feature_values_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_pm_fvh_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "CHECK"
  },
  {
   "columns": [
    "member_index"
   ],
   "definition": "CHECK (member_index >= 0)",
   "name": "ck_l2f_pm_member_index_nonneg",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "CHECK"
  },
  {
   "columns": [
    "partition"
   ],
   "definition": "CHECK (partition = 'train'::text)",
   "name": "ck_l2f_pm_partition_train",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "CHECK"
  },
  {
   "columns": [
    "feature_matrix_member_id",
    "feature_matrix_id",
    "dataset_registry_id",
    "member_index",
    "feature_values_hash"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (feature_matrix_member_id, feature_matrix_id, dataset_registry_id, member_index, feature_values_hash) REFERENCES profiling.feature_matrix_members(id, feature_matrix_id, dataset_registry_id, member_index, feature_values_hash)",
   "match": "SIMPLE",
   "name": "fk_l2f_pm_matrix_member",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "feature_matrix_id",
    "dataset_registry_id",
    "member_index",
    "feature_values_hash"
   ],
   "referred_schema": "profiling",
   "referred_table": "feature_matrix_members",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "plan_id",
    "profile_snapshot_id",
    "feature_matrix_id"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (plan_id, profile_snapshot_id, feature_matrix_id) REFERENCES experiments.l2f_experiment_plans(id, profile_snapshot_id, train_feature_matrix_id)",
   "match": "SIMPLE",
   "name": "fk_l2f_pm_plan_lineage",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "profile_snapshot_id",
    "train_feature_matrix_id"
   ],
   "referred_schema": "experiments",
   "referred_table": "l2f_experiment_plans",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "profile_snapshot_member_id",
    "profile_snapshot_id",
    "dataset_registry_id",
    "bam_profile_id",
    "partition",
    "feature_values_hash"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (profile_snapshot_member_id, profile_snapshot_id, dataset_registry_id, bam_profile_id, partition, feature_values_hash) REFERENCES profiling.profile_snapshot_members(id, profile_snapshot_id, dataset_registry_id, bam_profile_id, partition, feature_values_hash)",
   "match": "SIMPLE",
   "name": "fk_l2f_pm_snapshot_member",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "profile_snapshot_id",
    "dataset_registry_id",
    "bam_profile_id",
    "partition",
    "feature_values_hash"
   ],
   "referred_schema": "profiling",
   "referred_table": "profile_snapshot_members",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "id"
   ],
   "definition": "PRIMARY KEY (id)",
   "name": "pk_l2f_experiment_plan_members",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "PRIMARY KEY"
  },
  {
   "columns": [
    "id",
    "plan_id"
   ],
   "definition": "UNIQUE (id, plan_id)",
   "name": "uq_l2f_pm_id_plan",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "plan_id",
    "feature_matrix_member_id"
   ],
   "definition": "UNIQUE (plan_id, feature_matrix_member_id)",
   "name": "uq_l2f_pm_plan_matrix_member",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "plan_id",
    "member_index"
   ],
   "definition": "UNIQUE (plan_id, member_index)",
   "name": "uq_l2f_pm_plan_member_index",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "plan_id",
    "profile_snapshot_member_id"
   ],
   "definition": "UNIQUE (plan_id, profile_snapshot_member_id)",
   "name": "uq_l2f_pm_plan_snapshot_member",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "candidate_count"
   ],
   "definition": "CHECK (candidate_count >= 0)",
   "name": "ck_l2f_plans_candidate_count_nonneg",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "candidate_set_hash"
   ],
   "definition": "CHECK (candidate_set_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_candidate_set_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "feature_registry_hash"
   ],
   "definition": "CHECK (feature_registry_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_feature_registry_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "feature_set_hash"
   ],
   "definition": "CHECK (feature_set_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_feature_set_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "gatk_registry_hash"
   ],
   "definition": "CHECK (gatk_registry_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_gatk_registry_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "logical_job_count",
    "train_member_count",
    "candidate_count"
   ],
   "definition": "CHECK (logical_job_count = (train_member_count * candidate_count))",
   "name": "ck_l2f_plans_job_count_consistent",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "logical_job_count"
   ],
   "definition": "CHECK (logical_job_count >= 0)",
   "name": "ck_l2f_plans_job_count_nonneg",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "parameter_space_hash"
   ],
   "definition": "CHECK (parameter_space_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_param_space_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "partition"
   ],
   "definition": "CHECK (partition = 'train'::text)",
   "name": "ck_l2f_plans_partition_train",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "plan_hash"
   ],
   "definition": "CHECK (plan_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_plan_hash_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "experiment_parameter_policy_hash"
   ],
   "definition": "CHECK (experiment_parameter_policy_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_policy_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "registry_snapshot_hash"
   ],
   "definition": "CHECK (registry_snapshot_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_registry_snapshot_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "snapshot_hash"
   ],
   "definition": "CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_snapshot_hash_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "split_manifest_hash"
   ],
   "definition": "CHECK (split_manifest_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_split_hash_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "train_member_count"
   ],
   "definition": "CHECK (train_member_count >= 0)",
   "name": "ck_l2f_plans_train_count_nonneg",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "train_matrix_hash"
   ],
   "definition": "CHECK (train_matrix_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_train_matrix_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "train_feature_view_hash"
   ],
   "definition": "CHECK (train_feature_view_hash ~ '^[0-9a-f]{64}$'::text)",
   "name": "ck_l2f_plans_train_view_hex",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "CHECK"
  },
  {
   "columns": [
    "feature_set_id",
    "feature_set_hash",
    "feature_registry_hash"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (feature_set_id, feature_set_hash, feature_registry_hash) REFERENCES profiling.feature_sets(id, feature_set_hash, registry_hash)",
   "match": "SIMPLE",
   "name": "fk_l2f_plans_feature_set_identity",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "feature_set_hash",
    "registry_hash"
   ],
   "referred_schema": "profiling",
   "referred_table": "feature_sets",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "profile_snapshot_id",
    "snapshot_hash",
    "split_manifest_hash",
    "registry_snapshot_hash"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (profile_snapshot_id, snapshot_hash, split_manifest_hash, registry_snapshot_hash) REFERENCES profiling.profile_snapshots(id, snapshot_hash, split_manifest_hash, registry_snapshot_hash)",
   "match": "SIMPLE",
   "name": "fk_l2f_plans_snapshot_identity",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "snapshot_hash",
    "split_manifest_hash",
    "registry_snapshot_hash"
   ],
   "referred_schema": "profiling",
   "referred_table": "profile_snapshots",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "train_feature_matrix_id",
    "profile_snapshot_id",
    "partition",
    "train_matrix_hash",
    "feature_set_id"
   ],
   "deferrable": false,
   "deferred": false,
   "definition": "FOREIGN KEY (train_feature_matrix_id, profile_snapshot_id, partition, train_matrix_hash, feature_set_id) REFERENCES profiling.feature_matrices(id, profile_snapshot_id, partition, matrix_hash, feature_set_id)",
   "match": "SIMPLE",
   "name": "fk_l2f_plans_train_matrix_lineage",
   "on_delete": "NO ACTION",
   "on_update": "NO ACTION",
   "referred_columns": [
    "id",
    "profile_snapshot_id",
    "partition",
    "matrix_hash",
    "feature_set_id"
   ],
   "referred_schema": "profiling",
   "referred_table": "feature_matrices",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "FOREIGN KEY",
   "validated": true
  },
  {
   "columns": [
    "id"
   ],
   "definition": "PRIMARY KEY (id)",
   "name": "pk_l2f_experiment_plans",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "PRIMARY KEY"
  },
  {
   "columns": [
    "id",
    "parameter_space_hash"
   ],
   "definition": "UNIQUE (id, parameter_space_hash)",
   "name": "uq_l2f_plans_id_param_space",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "id",
    "profile_snapshot_id",
    "train_feature_matrix_id"
   ],
   "definition": "UNIQUE (id, profile_snapshot_id, train_feature_matrix_id)",
   "name": "uq_l2f_plans_id_snapshot_matrix",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "snapshot_hash",
    "split_manifest_hash",
    "registry_snapshot_hash",
    "train_matrix_hash",
    "train_feature_view_hash",
    "feature_set_hash",
    "feature_registry_hash",
    "gatk_registry_hash",
    "parameter_space_hash",
    "experiment_parameter_policy_hash",
    "candidate_set_hash"
   ],
   "definition": "UNIQUE (snapshot_hash, split_manifest_hash, registry_snapshot_hash, train_matrix_hash, train_feature_view_hash, feature_set_hash, feature_registry_hash, gatk_registry_hash, parameter_space_hash, experiment_parameter_policy_hash, candidate_set_hash)",
   "name": "uq_l2f_plans_logical_identity",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "UNIQUE"
  },
  {
   "columns": [
    "plan_hash"
   ],
   "definition": "UNIQUE (plan_hash)",
   "name": "uq_l2f_plans_plan_hash",
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "type": "UNIQUE"
  }
 ],
 "owned_indexes": [
  {
   "definition": "CREATE UNIQUE INDEX pk_l2f_config_payloads ON experiments.l2f_config_payloads USING btree (id)",
   "exclusion": false,
   "key_definitions": [
    "id"
   ],
   "method": "btree",
   "name": "pk_l2f_config_payloads",
   "predicate": null,
   "primary": true,
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_config_payloads_config_hash ON experiments.l2f_config_payloads USING btree (config_hash)",
   "exclusion": false,
   "key_definitions": [
    "config_hash"
   ],
   "method": "btree",
   "name": "uq_l2f_config_payloads_config_hash",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_config_payloads_id_hash_ps ON experiments.l2f_config_payloads USING btree (id, config_hash, parameter_space_hash)",
   "exclusion": false,
   "key_definitions": [
    "id",
    "config_hash",
    "parameter_space_hash"
   ],
   "method": "btree",
   "name": "uq_l2f_config_payloads_id_hash_ps",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_config_payloads",
   "unique": true
  },
  {
   "definition": "CREATE INDEX ix_l2f_jobs_status_created_at ON experiments.l2f_experiment_jobs USING btree (status, created_at)",
   "exclusion": false,
   "key_definitions": [
    "status",
    "created_at"
   ],
   "method": "btree",
   "name": "ix_l2f_jobs_status_created_at",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "unique": false
  },
  {
   "definition": "CREATE UNIQUE INDEX pk_l2f_experiment_jobs ON experiments.l2f_experiment_jobs USING btree (id)",
   "exclusion": false,
   "key_definitions": [
    "id"
   ],
   "method": "btree",
   "name": "pk_l2f_experiment_jobs",
   "predicate": null,
   "primary": true,
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_jobs_job_key ON experiments.l2f_experiment_jobs USING btree (job_key)",
   "exclusion": false,
   "key_definitions": [
    "job_key"
   ],
   "method": "btree",
   "name": "uq_l2f_jobs_job_key",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_jobs_logical_identity ON experiments.l2f_experiment_jobs USING btree (plan_id, plan_member_id, plan_config_id)",
   "exclusion": false,
   "key_definitions": [
    "plan_id",
    "plan_member_id",
    "plan_config_id"
   ],
   "method": "btree",
   "name": "uq_l2f_jobs_logical_identity",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_jobs",
   "unique": true
  },
  {
   "definition": "CREATE INDEX ix_l2f_pc_plan_id ON experiments.l2f_experiment_plan_configs USING btree (plan_id)",
   "exclusion": false,
   "key_definitions": [
    "plan_id"
   ],
   "method": "btree",
   "name": "ix_l2f_pc_plan_id",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "unique": false
  },
  {
   "definition": "CREATE UNIQUE INDEX pk_l2f_experiment_plan_configs ON experiments.l2f_experiment_plan_configs USING btree (id)",
   "exclusion": false,
   "key_definitions": [
    "id"
   ],
   "method": "btree",
   "name": "pk_l2f_experiment_plan_configs",
   "predicate": null,
   "primary": true,
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_pc_id_plan ON experiments.l2f_experiment_plan_configs USING btree (id, plan_id)",
   "exclusion": false,
   "key_definitions": [
    "id",
    "plan_id"
   ],
   "method": "btree",
   "name": "uq_l2f_pc_id_plan",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_pc_plan_index ON experiments.l2f_experiment_plan_configs USING btree (plan_id, config_index)",
   "exclusion": false,
   "key_definitions": [
    "plan_id",
    "config_index"
   ],
   "method": "btree",
   "name": "uq_l2f_pc_plan_index",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_pc_plan_payload ON experiments.l2f_experiment_plan_configs USING btree (plan_id, config_payload_id)",
   "exclusion": false,
   "key_definitions": [
    "plan_id",
    "config_payload_id"
   ],
   "method": "btree",
   "name": "uq_l2f_pc_plan_payload",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs",
   "unique": true
  },
  {
   "definition": "CREATE INDEX ix_l2f_pm_plan_id ON experiments.l2f_experiment_plan_members USING btree (plan_id)",
   "exclusion": false,
   "key_definitions": [
    "plan_id"
   ],
   "method": "btree",
   "name": "ix_l2f_pm_plan_id",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "unique": false
  },
  {
   "definition": "CREATE UNIQUE INDEX pk_l2f_experiment_plan_members ON experiments.l2f_experiment_plan_members USING btree (id)",
   "exclusion": false,
   "key_definitions": [
    "id"
   ],
   "method": "btree",
   "name": "pk_l2f_experiment_plan_members",
   "predicate": null,
   "primary": true,
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_pm_id_plan ON experiments.l2f_experiment_plan_members USING btree (id, plan_id)",
   "exclusion": false,
   "key_definitions": [
    "id",
    "plan_id"
   ],
   "method": "btree",
   "name": "uq_l2f_pm_id_plan",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_pm_plan_matrix_member ON experiments.l2f_experiment_plan_members USING btree (plan_id, feature_matrix_member_id)",
   "exclusion": false,
   "key_definitions": [
    "plan_id",
    "feature_matrix_member_id"
   ],
   "method": "btree",
   "name": "uq_l2f_pm_plan_matrix_member",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_pm_plan_member_index ON experiments.l2f_experiment_plan_members USING btree (plan_id, member_index)",
   "exclusion": false,
   "key_definitions": [
    "plan_id",
    "member_index"
   ],
   "method": "btree",
   "name": "uq_l2f_pm_plan_member_index",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_pm_plan_snapshot_member ON experiments.l2f_experiment_plan_members USING btree (plan_id, profile_snapshot_member_id)",
   "exclusion": false,
   "key_definitions": [
    "plan_id",
    "profile_snapshot_member_id"
   ],
   "method": "btree",
   "name": "uq_l2f_pm_plan_snapshot_member",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plan_members",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX pk_l2f_experiment_plans ON experiments.l2f_experiment_plans USING btree (id)",
   "exclusion": false,
   "key_definitions": [
    "id"
   ],
   "method": "btree",
   "name": "pk_l2f_experiment_plans",
   "predicate": null,
   "primary": true,
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_plans_id_param_space ON experiments.l2f_experiment_plans USING btree (id, parameter_space_hash)",
   "exclusion": false,
   "key_definitions": [
    "id",
    "parameter_space_hash"
   ],
   "method": "btree",
   "name": "uq_l2f_plans_id_param_space",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_plans_id_snapshot_matrix ON experiments.l2f_experiment_plans USING btree (id, profile_snapshot_id, train_feature_matrix_id)",
   "exclusion": false,
   "key_definitions": [
    "id",
    "profile_snapshot_id",
    "train_feature_matrix_id"
   ],
   "method": "btree",
   "name": "uq_l2f_plans_id_snapshot_matrix",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_plans_logical_identity ON experiments.l2f_experiment_plans USING btree (snapshot_hash, split_manifest_hash, registry_snapshot_hash, train_matrix_hash, train_feature_view_hash, feature_set_hash, feature_registry_hash, gatk_registry_hash, parameter_space_hash, experiment_parameter_policy_hash, candidate_set_hash)",
   "exclusion": false,
   "key_definitions": [
    "snapshot_hash",
    "split_manifest_hash",
    "registry_snapshot_hash",
    "train_matrix_hash",
    "train_feature_view_hash",
    "feature_set_hash",
    "feature_registry_hash",
    "gatk_registry_hash",
    "parameter_space_hash",
    "experiment_parameter_policy_hash",
    "candidate_set_hash"
   ],
   "method": "btree",
   "name": "uq_l2f_plans_logical_identity",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "unique": true
  },
  {
   "definition": "CREATE UNIQUE INDEX uq_l2f_plans_plan_hash ON experiments.l2f_experiment_plans USING btree (plan_hash)",
   "exclusion": false,
   "key_definitions": [
    "plan_hash"
   ],
   "method": "btree",
   "name": "uq_l2f_plans_plan_hash",
   "predicate": null,
   "primary": false,
   "schema": "experiments",
   "table": "l2f_experiment_plans",
   "unique": true
  }
 ],
 "owned_tables": {
  "experiments.l2f_config_payloads": {
   "acl_effective": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "acl_is_default": false,
   "acl_raw": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "column_acls": [],
   "columns": [
    {
     "collation": null,
     "default": "gen_random_uuid()",
     "generated": null,
     "identity": null,
     "name": "id",
     "notnull": true,
     "position": 1,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "config_hash",
     "notnull": true,
     "position": 2,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "parameter_space_hash",
     "notnull": true,
     "position": 3,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": "'l2f-config-payload-v1'::text",
     "generated": null,
     "identity": null,
     "name": "schema_version",
     "notnull": true,
     "position": 4,
     "type": "text"
    },
    {
     "collation": null,
     "default": "'application/vnd.minos.l2f-config+json'::text",
     "generated": null,
     "identity": null,
     "name": "media_type",
     "notnull": true,
     "position": 5,
     "type": "text"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "artifact_id",
     "notnull": true,
     "position": 6,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": "now()",
     "generated": null,
     "identity": null,
     "name": "created_at",
     "notnull": true,
     "position": 7,
     "type": "timestamp with time zone"
    }
   ],
   "kind": "table",
   "owner": "minos_admin",
   "persistence": "permanent",
   "reloptions": null,
   "replica_identity": "default",
   "rls_policies": [],
   "rowsecurity": false,
   "rowsecurity_forced": false
  },
  "experiments.l2f_experiment_jobs": {
   "acl_effective": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "acl_is_default": false,
   "acl_raw": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "column_acls": [],
   "columns": [
    {
     "collation": null,
     "default": "gen_random_uuid()",
     "generated": null,
     "identity": null,
     "name": "id",
     "notnull": true,
     "position": 1,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "plan_id",
     "notnull": true,
     "position": 2,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "plan_member_id",
     "notnull": true,
     "position": 3,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "plan_config_id",
     "notnull": true,
     "position": 4,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "job_key",
     "notnull": true,
     "position": 5,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": "'PENDING'::text",
     "generated": null,
     "identity": null,
     "name": "status",
     "notnull": true,
     "position": 6,
     "type": "text"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "claimed_by",
     "notnull": false,
     "position": 7,
     "type": "text"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "claimed_at",
     "notnull": false,
     "position": 8,
     "type": "timestamp with time zone"
    },
    {
     "collation": null,
     "default": "now()",
     "generated": null,
     "identity": null,
     "name": "created_at",
     "notnull": true,
     "position": 9,
     "type": "timestamp with time zone"
    },
    {
     "collation": null,
     "default": "now()",
     "generated": null,
     "identity": null,
     "name": "updated_at",
     "notnull": true,
     "position": 10,
     "type": "timestamp with time zone"
    }
   ],
   "kind": "table",
   "owner": "minos_admin",
   "persistence": "permanent",
   "reloptions": null,
   "replica_identity": "default",
   "rls_policies": [],
   "rowsecurity": false,
   "rowsecurity_forced": false
  },
  "experiments.l2f_experiment_plan_configs": {
   "acl_effective": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "acl_is_default": false,
   "acl_raw": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "column_acls": [],
   "columns": [
    {
     "collation": null,
     "default": "gen_random_uuid()",
     "generated": null,
     "identity": null,
     "name": "id",
     "notnull": true,
     "position": 1,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "plan_id",
     "notnull": true,
     "position": 2,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "config_payload_id",
     "notnull": true,
     "position": 3,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "config_hash",
     "notnull": true,
     "position": 4,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "parameter_space_hash",
     "notnull": true,
     "position": 5,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "config_index",
     "notnull": true,
     "position": 6,
     "type": "bigint"
    },
    {
     "collation": null,
     "default": "now()",
     "generated": null,
     "identity": null,
     "name": "created_at",
     "notnull": true,
     "position": 7,
     "type": "timestamp with time zone"
    }
   ],
   "kind": "table",
   "owner": "minos_admin",
   "persistence": "permanent",
   "reloptions": null,
   "replica_identity": "default",
   "rls_policies": [],
   "rowsecurity": false,
   "rowsecurity_forced": false
  },
  "experiments.l2f_experiment_plan_members": {
   "acl_effective": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "acl_is_default": false,
   "acl_raw": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "column_acls": [],
   "columns": [
    {
     "collation": null,
     "default": "gen_random_uuid()",
     "generated": null,
     "identity": null,
     "name": "id",
     "notnull": true,
     "position": 1,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "plan_id",
     "notnull": true,
     "position": 2,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "profile_snapshot_id",
     "notnull": true,
     "position": 3,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "feature_matrix_id",
     "notnull": true,
     "position": 4,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "profile_snapshot_member_id",
     "notnull": true,
     "position": 5,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "feature_matrix_member_id",
     "notnull": true,
     "position": 6,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "bam_profile_id",
     "notnull": true,
     "position": 7,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "dataset_registry_id",
     "notnull": true,
     "position": 8,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": "'train'::text",
     "generated": null,
     "identity": null,
     "name": "partition",
     "notnull": true,
     "position": 9,
     "type": "text"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "feature_values_hash",
     "notnull": true,
     "position": 10,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "member_index",
     "notnull": true,
     "position": 11,
     "type": "bigint"
    },
    {
     "collation": null,
     "default": "now()",
     "generated": null,
     "identity": null,
     "name": "created_at",
     "notnull": true,
     "position": 12,
     "type": "timestamp with time zone"
    }
   ],
   "kind": "table",
   "owner": "minos_admin",
   "persistence": "permanent",
   "reloptions": null,
   "replica_identity": "default",
   "rls_policies": [],
   "rowsecurity": false,
   "rowsecurity_forced": false
  },
  "experiments.l2f_experiment_plans": {
   "acl_effective": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "acl_is_default": false,
   "acl_raw": [
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "DELETE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "INSERT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "REFERENCES"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "SELECT"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRIGGER"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "TRUNCATE"
    },
    {
     "grantable": false,
     "grantee": "minos_admin",
     "grantor": "minos_admin",
     "privilege": "UPDATE"
    }
   ],
   "column_acls": [],
   "columns": [
    {
     "collation": null,
     "default": "gen_random_uuid()",
     "generated": null,
     "identity": null,
     "name": "id",
     "notnull": true,
     "position": 1,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "profile_snapshot_id",
     "notnull": true,
     "position": 2,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "train_feature_matrix_id",
     "notnull": true,
     "position": 3,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "feature_set_id",
     "notnull": true,
     "position": 4,
     "type": "uuid"
    },
    {
     "collation": null,
     "default": "'train'::text",
     "generated": null,
     "identity": null,
     "name": "partition",
     "notnull": true,
     "position": 5,
     "type": "text"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "snapshot_hash",
     "notnull": true,
     "position": 6,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "split_manifest_hash",
     "notnull": true,
     "position": 7,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "registry_snapshot_hash",
     "notnull": true,
     "position": 8,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "train_matrix_hash",
     "notnull": true,
     "position": 9,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "train_feature_view_hash",
     "notnull": true,
     "position": 10,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "feature_set_hash",
     "notnull": true,
     "position": 11,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "feature_registry_hash",
     "notnull": true,
     "position": 12,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "gatk_registry_hash",
     "notnull": true,
     "position": 13,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "parameter_space_hash",
     "notnull": true,
     "position": 14,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "experiment_parameter_policy_hash",
     "notnull": true,
     "position": 15,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "candidate_set_hash",
     "notnull": true,
     "position": 16,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "train_member_count",
     "notnull": true,
     "position": 17,
     "type": "bigint"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "candidate_count",
     "notnull": true,
     "position": 18,
     "type": "bigint"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "logical_job_count",
     "notnull": true,
     "position": 19,
     "type": "bigint"
    },
    {
     "collation": null,
     "default": null,
     "generated": null,
     "identity": null,
     "name": "plan_hash",
     "notnull": true,
     "position": 20,
     "type": "character(64)"
    },
    {
     "collation": null,
     "default": "now()",
     "generated": null,
     "identity": null,
     "name": "created_at",
     "notnull": true,
     "position": 21,
     "type": "timestamp with time zone"
    }
   ],
   "kind": "table",
   "owner": "minos_admin",
   "persistence": "permanent",
   "reloptions": null,
   "replica_identity": "default",
   "rls_policies": [],
   "rowsecurity": false,
   "rowsecurity_forced": false
  }
 },
 "owned_triggers": [
  {
   "definition": "CREATE TRIGGER trg_experiments_l2f_config_payloads_append_only BEFORE DELETE OR UPDATE ON experiments.l2f_config_payloads FOR EACH ROW EXECUTE FUNCTION audit.minos_reject_mutation()",
   "enabled": "O",
   "function": "audit.minos_reject_mutation",
   "internal": false,
   "name": "trg_experiments_l2f_config_payloads_append_only",
   "schema": "experiments",
   "table": "l2f_config_payloads"
  },
  {
   "definition": "CREATE TRIGGER trg_l2f_jobs_identity_immutable BEFORE UPDATE ON experiments.l2f_experiment_jobs FOR EACH ROW EXECUTE FUNCTION experiments.minos_l2f_reject_job_identity_change()",
   "enabled": "O",
   "function": "experiments.minos_l2f_reject_job_identity_change",
   "internal": false,
   "name": "trg_l2f_jobs_identity_immutable",
   "schema": "experiments",
   "table": "l2f_experiment_jobs"
  },
  {
   "definition": "CREATE TRIGGER trg_l2f_jobs_no_delete BEFORE DELETE ON experiments.l2f_experiment_jobs FOR EACH ROW EXECUTE FUNCTION audit.minos_reject_mutation()",
   "enabled": "O",
   "function": "audit.minos_reject_mutation",
   "internal": false,
   "name": "trg_l2f_jobs_no_delete",
   "schema": "experiments",
   "table": "l2f_experiment_jobs"
  },
  {
   "definition": "CREATE TRIGGER trg_experiments_l2f_experiment_plan_configs_append_only BEFORE DELETE OR UPDATE ON experiments.l2f_experiment_plan_configs FOR EACH ROW EXECUTE FUNCTION audit.minos_reject_mutation()",
   "enabled": "O",
   "function": "audit.minos_reject_mutation",
   "internal": false,
   "name": "trg_experiments_l2f_experiment_plan_configs_append_only",
   "schema": "experiments",
   "table": "l2f_experiment_plan_configs"
  },
  {
   "definition": "CREATE TRIGGER trg_experiments_l2f_experiment_plan_members_append_only BEFORE DELETE OR UPDATE ON experiments.l2f_experiment_plan_members FOR EACH ROW EXECUTE FUNCTION audit.minos_reject_mutation()",
   "enabled": "O",
   "function": "audit.minos_reject_mutation",
   "internal": false,
   "name": "trg_experiments_l2f_experiment_plan_members_append_only",
   "schema": "experiments",
   "table": "l2f_experiment_plan_members"
  },
  {
   "definition": "CREATE TRIGGER trg_experiments_l2f_experiment_plans_append_only BEFORE DELETE OR UPDATE ON experiments.l2f_experiment_plans FOR EACH ROW EXECUTE FUNCTION audit.minos_reject_mutation()",
   "enabled": "O",
   "function": "audit.minos_reject_mutation",
   "internal": false,
   "name": "trg_experiments_l2f_experiment_plans_append_only",
   "schema": "experiments",
   "table": "l2f_experiment_plans"
  }
 ]
}"""

#: Exhaustive normalized inventory of every deployed L2-F schema object.
L2F_LIVE_INVENTORY: dict[str, Any] = json.loads(_L2F_LIVE_INVENTORY_JSON)

#: The complete frozen static inventory (static facts + the exhaustive live detail).
L2F_STATIC_INVENTORY: dict[str, Any] = {
    "schema_version": "l2f-static-inventory-v1",
    "revision": L2F_MIGRATION_REVISION,
    "down_revision": L2F_DOWN_REVISION,
    "owned_tables": list(L2F_TABLES),
    "external_stub_tables": list(L2F_EXTERNAL_STUB_TABLES),
    "composite_target_names": [t[2] for t in L2F_COMPOSITE_TARGETS],
    "composite_fk_names": list(L2F_COMPOSITE_FKS),
    "trigger_names": list(L2F_TRIGGERS),
    "job_function": L2F_JOB_FUNCTION,
    "plan_logical_identity": list(L2F_PLAN_LOGICAL_IDENTITY),
    "config_payload_schema": L2F_CONFIG_PAYLOAD_SCHEMA,
    "config_payload_media_type": L2F_CONFIG_PAYLOAD_MEDIA_TYPE,
    "no_app_role_grants": True,
    "live": L2F_LIVE_INVENTORY,
}

#: Frozen byte SHA-256 of the authoritative migration file.
L2F_MIGRATION_SHA256 = "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6"

CONTRACT_DOMAIN = "minos.l2f.migration-contract"
CONTRACT_VERSION = "v1"


def compute_migration_sha256() -> str:
    """Recompute the byte SHA-256 of the authoritative 0006 migration file."""
    return hashlib.sha256(Path(L2F_MIGRATION_FILE).read_bytes()).hexdigest()


def contract_preimage(
    *,
    migration_sha256: str,
    revision: str = L2F_MIGRATION_REVISION,
    down_revision: str = L2F_DOWN_REVISION,
    prior_migration_shas: dict[str, str] | None = None,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the (non-self-referential) contract-hash preimage. All inputs are overridable so
    mutation tests can perturb any field without monkeypatching module globals."""
    return {
        "domain": CONTRACT_DOMAIN,
        "version": CONTRACT_VERSION,
        "migration_sha256": migration_sha256,
        "revision": revision,
        "down_revision": down_revision,
        "prior_migration_shas": ACCEPTED_PRIOR_MIGRATION_SHAS
        if prior_migration_shas is None
        else prior_migration_shas,
        "inventory": L2F_STATIC_INVENTORY if inventory is None else inventory,
    }


def compute_contract_hash(
    *,
    migration_sha256: str,
    revision: str = L2F_MIGRATION_REVISION,
    down_revision: str = L2F_DOWN_REVISION,
    prior_migration_shas: dict[str, str] | None = None,
    inventory: dict[str, Any] | None = None,
) -> str:
    """Domain-separated canonical hash over the migration SHA + revision lineage + prior
    migration hashes + the full static inventory."""
    return canonical_hash(
        contract_preimage(
            migration_sha256=migration_sha256,
            revision=revision,
            down_revision=down_revision,
            prior_migration_shas=prior_migration_shas,
            inventory=inventory,
        )
    )


#: Frozen contract hash over the migration bytes + full static inventory.
L2F_CONTRACT_HASH = "c7a2e978857830ccff67821ded1196472d5f38baacb19a64352ec686ce74916b"


def l2f_contract_hash(migration_file_sha256: str) -> str:
    """Legacy compact contract hash (retained for backward compatibility)."""
    return canonical_hash(
        {"migration_file_sha256": migration_file_sha256, "inventory": L2F_INVENTORY}
    )


def migration_file_sha256() -> str:
    return compute_migration_sha256()
