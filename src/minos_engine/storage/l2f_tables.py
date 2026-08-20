"""Private SQLAlchemy Core mappings for the L2-F experiment-plan tables (F3-A).

Migration ``0006_l2f_experiment_plan`` is the AUTHORITATIVE DDL. These Core ``Table``
objects mirror it 1:1 for later F3-C inserts/selects, but live on a DEDICATED private
``l2f_metadata`` — they are **never** added to the L2-B declarative ``Base.metadata`` (that
would silently change the accepted DB-READY storage fingerprint) and ``create_all`` /
``drop_all`` are never called on them.

External L2-D/L2-E tables referenced by composite FKs are declared as minimal STUBS on the
same private metadata (only the columns needed to resolve the FK targets), tagged
``info={"l2f_external_target_stub": True}``. Stubs are metadata declarations only and are
never treated as L2-F-owned tables.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from minos_engine.storage.l2f_migration_contract import (
    L2F_CONFIG_PAYLOAD_MEDIA_TYPE,
    L2F_CONFIG_PAYLOAD_SCHEMA,
)

__all__ = [
    "l2f_metadata",
    "L2F_OWNED_TABLES",
    "L2F_EXTERNAL_TARGET_STUBS",
    "l2f_experiment_plans",
    "l2f_experiment_plan_members",
    "l2f_config_payloads",
    "l2f_experiment_plan_configs",
    "l2f_experiment_jobs",
]

l2f_metadata = sa.MetaData()

_HEX = "^[0-9a-f]{64}$"
_STUB = {"l2f_external_target_stub": True}


def _uuid(name: str, *, nullable: bool = False, **kw: Any) -> sa.Column[Any]:
    # matches the migration's _uuid default (NOT NULL) for all owned identity/FK columns.
    return sa.Column(name, postgresql.UUID(as_uuid=False), nullable=nullable, **kw)


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=False),
        nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def _sha(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.CHAR(64), nullable=False)


def _ts(name: str = "created_at") -> sa.Column[Any]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _hex_ck(col: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} ~ '{_HEX}'", name=name)


# --------------------------------------------------------------------------- #
# external target stubs (metadata-only; never created/dropped/owned by L2-F)
# --------------------------------------------------------------------------- #
sa.Table(
    "profile_snapshots",
    l2f_metadata,
    _uuid("id"),
    _sha("snapshot_hash"),
    _sha("split_manifest_hash"),
    _sha("registry_snapshot_hash"),
    sa.UniqueConstraint(
        "id",
        "snapshot_hash",
        "split_manifest_hash",
        "registry_snapshot_hash",
        name="uq_l2f_profile_snapshots_composite",
    ),
    schema="profiling",
    info=_STUB,
)
sa.Table(
    "feature_sets",
    l2f_metadata,
    _uuid("id"),
    _sha("feature_set_hash"),
    _sha("registry_hash"),
    sa.UniqueConstraint(
        "id", "feature_set_hash", "registry_hash", name="uq_l2f_feature_sets_composite"
    ),
    schema="profiling",
    info=_STUB,
)
sa.Table(
    "feature_matrices",
    l2f_metadata,
    _uuid("id"),
    _uuid("profile_snapshot_id"),
    sa.Column("partition", sa.Text()),
    _sha("matrix_hash"),
    _uuid("feature_set_id"),
    sa.UniqueConstraint(
        "id",
        "profile_snapshot_id",
        "partition",
        "matrix_hash",
        "feature_set_id",
        name="uq_l2f_feature_matrices_composite",
    ),
    schema="profiling",
    info=_STUB,
)
sa.Table(
    "profile_snapshot_members",
    l2f_metadata,
    _uuid("id"),
    _uuid("profile_snapshot_id"),
    _uuid("dataset_registry_id"),
    _uuid("bam_profile_id"),
    sa.Column("partition", sa.Text()),
    _sha("feature_values_hash"),
    sa.UniqueConstraint(
        "id",
        "profile_snapshot_id",
        "dataset_registry_id",
        "bam_profile_id",
        "partition",
        "feature_values_hash",
        name="uq_l2f_psm_composite",
    ),
    schema="profiling",
    info=_STUB,
)
sa.Table(
    "feature_matrix_members",
    l2f_metadata,
    _uuid("id"),
    _uuid("feature_matrix_id"),
    _uuid("dataset_registry_id"),
    sa.Column("member_index", sa.BigInteger()),
    _sha("feature_values_hash"),
    sa.UniqueConstraint(
        "id",
        "feature_matrix_id",
        "dataset_registry_id",
        "member_index",
        "feature_values_hash",
        name="uq_l2f_fmm_composite",
    ),
    schema="profiling",
    info=_STUB,
)
sa.Table(
    "artifacts",
    l2f_metadata,
    _uuid("id"),
    _sha("sha256"),
    sa.Column("media_type", sa.Text()),
    sa.UniqueConstraint("id", "sha256", "media_type", name="uq_l2f_artifacts_id_sha_media"),
    schema="catalog",
    info=_STUB,
)


# --------------------------------------------------------------------------- #
# owned L2-F tables (mirror migration 0006 exactly)
# --------------------------------------------------------------------------- #
l2f_experiment_plans = sa.Table(
    "l2f_experiment_plans",
    l2f_metadata,
    _uuid_pk(),
    _uuid("profile_snapshot_id"),
    _uuid("train_feature_matrix_id"),
    _uuid("feature_set_id"),
    sa.Column("partition", sa.Text(), nullable=False, server_default="train"),
    _sha("snapshot_hash"),
    _sha("split_manifest_hash"),
    _sha("registry_snapshot_hash"),
    _sha("train_matrix_hash"),
    _sha("train_feature_view_hash"),
    _sha("feature_set_hash"),
    _sha("feature_registry_hash"),
    _sha("gatk_registry_hash"),
    _sha("parameter_space_hash"),
    _sha("experiment_parameter_policy_hash"),
    _sha("candidate_set_hash"),
    sa.Column("train_member_count", sa.BigInteger(), nullable=False),
    sa.Column("candidate_count", sa.BigInteger(), nullable=False),
    sa.Column("logical_job_count", sa.BigInteger(), nullable=False),
    _sha("plan_hash"),
    _ts(),
    sa.PrimaryKeyConstraint("id", name="pk_l2f_experiment_plans"),
    sa.UniqueConstraint("plan_hash", name="uq_l2f_plans_plan_hash"),
    sa.UniqueConstraint(
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
        name="uq_l2f_plans_logical_identity",
    ),
    sa.UniqueConstraint(
        "id",
        "profile_snapshot_id",
        "train_feature_matrix_id",
        name="uq_l2f_plans_id_snapshot_matrix",
    ),
    sa.UniqueConstraint("id", "parameter_space_hash", name="uq_l2f_plans_id_param_space"),
    sa.ForeignKeyConstraint(
        ["profile_snapshot_id", "snapshot_hash", "split_manifest_hash", "registry_snapshot_hash"],
        [
            "profiling.profile_snapshots.id",
            "profiling.profile_snapshots.snapshot_hash",
            "profiling.profile_snapshots.split_manifest_hash",
            "profiling.profile_snapshots.registry_snapshot_hash",
        ],
        name="fk_l2f_plans_snapshot_identity",
    ),
    sa.ForeignKeyConstraint(
        ["feature_set_id", "feature_set_hash", "feature_registry_hash"],
        [
            "profiling.feature_sets.id",
            "profiling.feature_sets.feature_set_hash",
            "profiling.feature_sets.registry_hash",
        ],
        name="fk_l2f_plans_feature_set_identity",
    ),
    sa.ForeignKeyConstraint(
        [
            "train_feature_matrix_id",
            "profile_snapshot_id",
            "partition",
            "train_matrix_hash",
            "feature_set_id",
        ],
        [
            "profiling.feature_matrices.id",
            "profiling.feature_matrices.profile_snapshot_id",
            "profiling.feature_matrices.partition",
            "profiling.feature_matrices.matrix_hash",
            "profiling.feature_matrices.feature_set_id",
        ],
        name="fk_l2f_plans_train_matrix_lineage",
    ),
    sa.CheckConstraint("partition = 'train'", name="ck_l2f_plans_partition_train"),
    sa.CheckConstraint("train_member_count >= 0", name="ck_l2f_plans_train_count_nonneg"),
    sa.CheckConstraint("candidate_count >= 0", name="ck_l2f_plans_candidate_count_nonneg"),
    sa.CheckConstraint("logical_job_count >= 0", name="ck_l2f_plans_job_count_nonneg"),
    sa.CheckConstraint(
        "logical_job_count = train_member_count * candidate_count",
        name="ck_l2f_plans_job_count_consistent",
    ),
    _hex_ck("snapshot_hash", "ck_l2f_plans_snapshot_hash_hex"),
    _hex_ck("split_manifest_hash", "ck_l2f_plans_split_hash_hex"),
    _hex_ck("registry_snapshot_hash", "ck_l2f_plans_registry_snapshot_hex"),
    _hex_ck("train_matrix_hash", "ck_l2f_plans_train_matrix_hex"),
    _hex_ck("train_feature_view_hash", "ck_l2f_plans_train_view_hex"),
    _hex_ck("feature_set_hash", "ck_l2f_plans_feature_set_hex"),
    _hex_ck("feature_registry_hash", "ck_l2f_plans_feature_registry_hex"),
    _hex_ck("gatk_registry_hash", "ck_l2f_plans_gatk_registry_hex"),
    _hex_ck("parameter_space_hash", "ck_l2f_plans_param_space_hex"),
    _hex_ck("experiment_parameter_policy_hash", "ck_l2f_plans_policy_hex"),
    _hex_ck("candidate_set_hash", "ck_l2f_plans_candidate_set_hex"),
    _hex_ck("plan_hash", "ck_l2f_plans_plan_hash_hex"),
    schema="experiments",
)

l2f_experiment_plan_members = sa.Table(
    "l2f_experiment_plan_members",
    l2f_metadata,
    _uuid_pk(),
    _uuid("plan_id"),
    _uuid("profile_snapshot_id"),
    _uuid("feature_matrix_id"),
    _uuid("profile_snapshot_member_id"),
    _uuid("feature_matrix_member_id"),
    _uuid("bam_profile_id"),
    _uuid("dataset_registry_id"),
    sa.Column("partition", sa.Text(), nullable=False, server_default="train"),
    _sha("feature_values_hash"),
    sa.Column("member_index", sa.BigInteger(), nullable=False),
    _ts(),
    sa.PrimaryKeyConstraint("id", name="pk_l2f_experiment_plan_members"),
    sa.ForeignKeyConstraint(
        ["plan_id", "profile_snapshot_id", "feature_matrix_id"],
        [
            "experiments.l2f_experiment_plans.id",
            "experiments.l2f_experiment_plans.profile_snapshot_id",
            "experiments.l2f_experiment_plans.train_feature_matrix_id",
        ],
        name="fk_l2f_pm_plan_lineage",
    ),
    sa.ForeignKeyConstraint(
        [
            "profile_snapshot_member_id",
            "profile_snapshot_id",
            "dataset_registry_id",
            "bam_profile_id",
            "partition",
            "feature_values_hash",
        ],
        [
            "profiling.profile_snapshot_members.id",
            "profiling.profile_snapshot_members.profile_snapshot_id",
            "profiling.profile_snapshot_members.dataset_registry_id",
            "profiling.profile_snapshot_members.bam_profile_id",
            "profiling.profile_snapshot_members.partition",
            "profiling.profile_snapshot_members.feature_values_hash",
        ],
        name="fk_l2f_pm_snapshot_member",
    ),
    sa.ForeignKeyConstraint(
        [
            "feature_matrix_member_id",
            "feature_matrix_id",
            "dataset_registry_id",
            "member_index",
            "feature_values_hash",
        ],
        [
            "profiling.feature_matrix_members.id",
            "profiling.feature_matrix_members.feature_matrix_id",
            "profiling.feature_matrix_members.dataset_registry_id",
            "profiling.feature_matrix_members.member_index",
            "profiling.feature_matrix_members.feature_values_hash",
        ],
        name="fk_l2f_pm_matrix_member",
    ),
    sa.UniqueConstraint(
        "plan_id", "profile_snapshot_member_id", name="uq_l2f_pm_plan_snapshot_member"
    ),
    sa.UniqueConstraint("plan_id", "feature_matrix_member_id", name="uq_l2f_pm_plan_matrix_member"),
    sa.UniqueConstraint("plan_id", "member_index", name="uq_l2f_pm_plan_member_index"),
    sa.UniqueConstraint("id", "plan_id", name="uq_l2f_pm_id_plan"),
    sa.CheckConstraint("partition = 'train'", name="ck_l2f_pm_partition_train"),
    sa.CheckConstraint("member_index >= 0", name="ck_l2f_pm_member_index_nonneg"),
    _hex_ck("feature_values_hash", "ck_l2f_pm_fvh_hex"),
    sa.Index("ix_l2f_pm_plan_id", "plan_id"),
    schema="experiments",
)

l2f_config_payloads = sa.Table(
    "l2f_config_payloads",
    l2f_metadata,
    _uuid_pk(),
    _sha("config_hash"),
    _sha("parameter_space_hash"),
    sa.Column(
        "schema_version", sa.Text(), nullable=False, server_default=L2F_CONFIG_PAYLOAD_SCHEMA
    ),
    sa.Column(
        "media_type", sa.Text(), nullable=False, server_default=L2F_CONFIG_PAYLOAD_MEDIA_TYPE
    ),
    _uuid("artifact_id"),
    _ts(),
    sa.PrimaryKeyConstraint("id", name="pk_l2f_config_payloads"),
    sa.UniqueConstraint("config_hash", name="uq_l2f_config_payloads_config_hash"),
    sa.UniqueConstraint(
        "id", "config_hash", "parameter_space_hash", name="uq_l2f_config_payloads_id_hash_ps"
    ),
    sa.ForeignKeyConstraint(
        ["artifact_id", "config_hash", "media_type"],
        ["catalog.artifacts.id", "catalog.artifacts.sha256", "catalog.artifacts.media_type"],
        name="fk_l2f_cp_artifact_sha_media",
    ),
    sa.CheckConstraint(
        f"schema_version = '{L2F_CONFIG_PAYLOAD_SCHEMA}'", name="ck_l2f_cp_schema_version"
    ),
    sa.CheckConstraint(
        f"media_type = '{L2F_CONFIG_PAYLOAD_MEDIA_TYPE}'", name="ck_l2f_cp_media_type"
    ),
    _hex_ck("config_hash", "ck_l2f_cp_config_hash_hex"),
    _hex_ck("parameter_space_hash", "ck_l2f_cp_param_space_hex"),
    schema="experiments",
)

l2f_experiment_plan_configs = sa.Table(
    "l2f_experiment_plan_configs",
    l2f_metadata,
    _uuid_pk(),
    _uuid("plan_id"),
    _uuid("config_payload_id"),
    _sha("config_hash"),
    _sha("parameter_space_hash"),
    sa.Column("config_index", sa.BigInteger(), nullable=False),
    _ts(),
    sa.PrimaryKeyConstraint("id", name="pk_l2f_experiment_plan_configs"),
    sa.ForeignKeyConstraint(
        ["plan_id", "parameter_space_hash"],
        [
            "experiments.l2f_experiment_plans.id",
            "experiments.l2f_experiment_plans.parameter_space_hash",
        ],
        name="fk_l2f_pc_plan_param_space",
    ),
    sa.ForeignKeyConstraint(
        ["config_payload_id", "config_hash", "parameter_space_hash"],
        [
            "experiments.l2f_config_payloads.id",
            "experiments.l2f_config_payloads.config_hash",
            "experiments.l2f_config_payloads.parameter_space_hash",
        ],
        name="fk_l2f_pc_payload_identity",
    ),
    sa.UniqueConstraint("plan_id", "config_payload_id", name="uq_l2f_pc_plan_payload"),
    sa.UniqueConstraint("plan_id", "config_index", name="uq_l2f_pc_plan_index"),
    sa.UniqueConstraint("id", "plan_id", name="uq_l2f_pc_id_plan"),
    sa.CheckConstraint("config_index >= 0", name="ck_l2f_pc_config_index_nonneg"),
    _hex_ck("config_hash", "ck_l2f_pc_config_hash_hex"),
    _hex_ck("parameter_space_hash", "ck_l2f_pc_param_space_hex"),
    sa.Index("ix_l2f_pc_plan_id", "plan_id"),
    schema="experiments",
)

l2f_experiment_jobs = sa.Table(
    "l2f_experiment_jobs",
    l2f_metadata,
    _uuid_pk(),
    _uuid("plan_id"),
    _uuid("plan_member_id"),
    _uuid("plan_config_id"),
    sa.Column("job_key", sa.CHAR(64), nullable=False),
    sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
    sa.Column("claimed_by", sa.Text(), nullable=True),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    _ts(),
    _ts("updated_at"),
    sa.PrimaryKeyConstraint("id", name="pk_l2f_experiment_jobs"),
    sa.ForeignKeyConstraint(
        ["plan_id"], ["experiments.l2f_experiment_plans.id"], name="fk_l2f_job_plan_id"
    ),
    sa.ForeignKeyConstraint(
        ["plan_member_id", "plan_id"],
        [
            "experiments.l2f_experiment_plan_members.id",
            "experiments.l2f_experiment_plan_members.plan_id",
        ],
        name="fk_l2f_job_member_plan",
    ),
    sa.ForeignKeyConstraint(
        ["plan_config_id", "plan_id"],
        [
            "experiments.l2f_experiment_plan_configs.id",
            "experiments.l2f_experiment_plan_configs.plan_id",
        ],
        name="fk_l2f_job_config_plan",
    ),
    sa.UniqueConstraint("job_key", name="uq_l2f_jobs_job_key"),
    sa.UniqueConstraint(
        "plan_id", "plan_member_id", "plan_config_id", name="uq_l2f_jobs_logical_identity"
    ),
    sa.CheckConstraint(
        "status IN ('PENDING', 'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
        name="ck_l2f_jobs_status_valid",
    ),
    _hex_ck("job_key", "ck_l2f_jobs_job_key_hex"),
    sa.Index("ix_l2f_jobs_status_created_at", "status", "created_at"),
    schema="experiments",
)

#: the five L2-F-owned tables (usable by future F3 persistence code).
L2F_OWNED_TABLES = (
    l2f_experiment_plans,
    l2f_experiment_plan_members,
    l2f_config_payloads,
    l2f_experiment_plan_configs,
    l2f_experiment_jobs,
)

#: external target stubs — metadata-only; never created/dropped/owned by L2-F.
L2F_EXTERNAL_TARGET_STUBS = tuple(
    t for t in l2f_metadata.tables.values() if t.info.get("l2f_external_target_stub")
)
