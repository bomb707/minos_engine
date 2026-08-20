"""L2-F F3-A: experiment-plan schema (additive; 5 tables + declarative train lineage).

Additive migration over the accepted L2-E head ``0005_l2e_feature_view``. It creates the
five canonical L2-F tables in the ``experiments`` schema and — so that train-only
membership and config/plan lineage are proven at the PostgreSQL boundary, not merely in
Python — it adds reversible composite UNIQUE constraints to the immutable 0004/0005
tables to serve as composite-FK targets. It NEVER edits migrations 0001-0005, never
touches legacy ``profiling.profiles`` / ``experiments.jobs`` / ``experiments.results``,
and its downgrade restores exactly the 0005 runtime schema + grants.

Every cross-table invariant is enforced DECLARATIVELY via composite foreign keys:

  plan → exact profile snapshot + exact TRAIN feature matrix (partition fixed to 'train');
  plan member → same plan snapshot+matrix, exact snapshot member (partition 'train',
      dataset_registry, bam_profile, feature_values_hash), exact matrix member
      (feature_matrix, dataset_registry, member_index, feature_values_hash) — sharing the
      member's dataset_registry_id + feature_values_hash across BOTH composite FKs forces
      them equal;
  plan config → exact stored config payload (artifact sha256 == config_hash);
  job → plan member + plan config that both belong to the job's plan.

The only trigger is row-immutability: fully-immutable tables reuse the existing
``audit.minos_reject_mutation`` append-only function; the job table uses a new
identity-change trigger so status/claim metadata may change while scientific identity
cannot. F4 (claim/execution) is unauthorized here — no claim function, no grants to
minos_runner.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_l2f_experiment_plan"
down_revision: str | None = "0005_l2e_feature_view"
branch_labels = None
depends_on = None

_HEX = "^[0-9a-f]{64}$"
_JOB_STATUSES = ("PENDING", "CLAIMED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
_JOB_IDENTITY = "experiments.minos_l2f_reject_job_identity_change"
_JOB_IDENTITY_COLS = ("id", "plan_id", "plan_member_id", "plan_config_id", "job_key", "created_at")

# additive composite UNIQUE targets on existing immutable tables (added here, dropped on
# downgrade) — the composite-FK targets that make the train lineage declarative.
_COMPOSITE_TARGETS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "profiling",
        "feature_matrices",
        "uq_l2f_feature_matrices_composite",
        ("id", "profile_snapshot_id", "partition", "matrix_hash", "feature_set_id"),
    ),
    (
        "profiling",
        "profile_snapshots",
        "uq_l2f_profile_snapshots_composite",
        ("id", "snapshot_hash", "split_manifest_hash", "registry_snapshot_hash"),
    ),
    (
        "profiling",
        "feature_sets",
        "uq_l2f_feature_sets_composite",
        ("id", "feature_set_hash", "registry_hash"),
    ),
    (
        "profiling",
        "profile_snapshot_members",
        "uq_l2f_psm_composite",
        (
            "id",
            "profile_snapshot_id",
            "dataset_registry_id",
            "bam_profile_id",
            "partition",
            "feature_values_hash",
        ),
    ),
    (
        "profiling",
        "feature_matrix_members",
        "uq_l2f_fmm_composite",
        ("id", "feature_matrix_id", "dataset_registry_id", "member_index", "feature_values_hash"),
    ),
    ("catalog", "artifacts", "uq_l2f_artifacts_id_sha_media", ("id", "sha256", "media_type")),
)

#: Frozen CONFIG-payload artifact identity (F5 reconstructs the exact CONFIG from these).
_CONFIG_PAYLOAD_SCHEMA = "l2f-config-payload-v1"
_CONFIG_PAYLOAD_MEDIA_TYPE = "application/vnd.minos.l2f-config+json"

_L2F_TABLES = (
    "l2f_experiment_jobs",
    "l2f_experiment_plan_configs",
    "l2f_experiment_plan_members",
    "l2f_config_payloads",
    "l2f_experiment_plans",
)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=False),
        nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=False), nullable=nullable)


def _ts(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _sha(name: str) -> sa.Column:
    return sa.Column(name, sa.CHAR(64), nullable=False)


def _hex_ck(col: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} ~ '{_HEX}'", name=name)


def _add_composite_targets() -> None:
    for schema, table, name, cols in _COMPOSITE_TARGETS:
        op.create_unique_constraint(name, table, list(cols), schema=schema)


def _drop_composite_targets() -> None:
    for schema, table, name, _cols in _COMPOSITE_TARGETS:
        op.drop_constraint(name, table, schema=schema, type_="unique")


def _create_plans() -> None:
    op.create_table(
        "l2f_experiment_plans",
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
        # the COMPLETE logical plan identity (all scientific identities; counts are derived
        # and re-verified by the future F3-B constructor / F3-D verifier).
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
        # composite-UNIQUE target for plan-member lineage FK.
        sa.UniqueConstraint(
            "id",
            "profile_snapshot_id",
            "train_feature_matrix_id",
            name="uq_l2f_plans_id_snapshot_matrix",
        ),
        # composite-UNIQUE target for the plan-config parameter-space FK.
        sa.UniqueConstraint("id", "parameter_space_hash", name="uq_l2f_plans_id_param_space"),
        # the snapshot MUST be the exact accepted snapshot: id + all three snapshot hashes
        # (a valid id with any forged snapshot hash is rejected declaratively).
        sa.ForeignKeyConstraint(
            [
                "profile_snapshot_id",
                "snapshot_hash",
                "split_manifest_hash",
                "registry_snapshot_hash",
            ],
            [
                "profiling.profile_snapshots.id",
                "profiling.profile_snapshots.snapshot_hash",
                "profiling.profile_snapshots.split_manifest_hash",
                "profiling.profile_snapshots.registry_snapshot_hash",
            ],
            name="fk_l2f_plans_snapshot_identity",
        ),
        # the feature set MUST be the exact accepted set: id + feature_set_hash + the L2-A
        # feature registry_hash (distinct from the GATK parameter-registry hash).
        sa.ForeignKeyConstraint(
            ["feature_set_id", "feature_set_hash", "feature_registry_hash"],
            [
                "profiling.feature_sets.id",
                "profiling.feature_sets.feature_set_hash",
                "profiling.feature_sets.registry_hash",
            ],
            name="fk_l2f_plans_feature_set_identity",
        ),
        # the train matrix MUST belong to this snapshot, have partition='train', and carry
        # the recorded matrix_hash + feature_set_id (declarative — no caller partition trust).
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


def _create_plan_members() -> None:
    op.create_table(
        "l2f_experiment_plan_members",
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
        # member's snapshot_id + matrix_id MUST equal the plan's (declarative).
        sa.ForeignKeyConstraint(
            ["plan_id", "profile_snapshot_id", "feature_matrix_id"],
            [
                "experiments.l2f_experiment_plans.id",
                "experiments.l2f_experiment_plans.profile_snapshot_id",
                "experiments.l2f_experiment_plans.train_feature_matrix_id",
            ],
            name="fk_l2f_pm_plan_lineage",
        ),
        # the snapshot member MUST be train, of this snapshot/dataset/bam_profile/fvh.
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
        # the matrix member MUST be of this matrix/dataset/index/fvh (shared dataset_id +
        # feature_values_hash with the snapshot-member FK forces cross-lineage equality).
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
        sa.UniqueConstraint(
            "plan_id", "feature_matrix_member_id", name="uq_l2f_pm_plan_matrix_member"
        ),
        sa.UniqueConstraint("plan_id", "member_index", name="uq_l2f_pm_plan_member_index"),
        # composite-UNIQUE target for the job FK.
        sa.UniqueConstraint("id", "plan_id", name="uq_l2f_pm_id_plan"),
        sa.CheckConstraint("partition = 'train'", name="ck_l2f_pm_partition_train"),
        sa.CheckConstraint("member_index >= 0", name="ck_l2f_pm_member_index_nonneg"),
        _hex_ck("feature_values_hash", "ck_l2f_pm_fvh_hex"),
        schema="experiments",
    )
    op.create_index(
        "ix_l2f_pm_plan_id", "l2f_experiment_plan_members", ["plan_id"], schema="experiments"
    )


def _create_config_payloads() -> None:
    op.create_table(
        "l2f_config_payloads",
        _uuid_pk(),
        _sha("config_hash"),
        _sha("parameter_space_hash"),
        sa.Column(
            "schema_version", sa.Text(), nullable=False, server_default=_CONFIG_PAYLOAD_SCHEMA
        ),
        sa.Column(
            "media_type", sa.Text(), nullable=False, server_default=_CONFIG_PAYLOAD_MEDIA_TYPE
        ),
        _uuid("artifact_id"),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_l2f_config_payloads"),
        sa.UniqueConstraint("config_hash", name="uq_l2f_config_payloads_config_hash"),
        # composite-UNIQUE target for the plan-config (config_hash + parameter_space) FK.
        sa.UniqueConstraint(
            "id", "config_hash", "parameter_space_hash", name="uq_l2f_config_payloads_id_hash_ps"
        ),
        # the referenced artifact's sha256 MUST equal config_hash AND its media_type MUST be
        # the canonical L2-F CONFIG media type (declarative payload + schema bind).
        sa.ForeignKeyConstraint(
            ["artifact_id", "config_hash", "media_type"],
            ["catalog.artifacts.id", "catalog.artifacts.sha256", "catalog.artifacts.media_type"],
            name="fk_l2f_cp_artifact_sha_media",
        ),
        sa.CheckConstraint(
            f"schema_version = '{_CONFIG_PAYLOAD_SCHEMA}'", name="ck_l2f_cp_schema_version"
        ),
        sa.CheckConstraint(
            f"media_type = '{_CONFIG_PAYLOAD_MEDIA_TYPE}'", name="ck_l2f_cp_media_type"
        ),
        _hex_ck("config_hash", "ck_l2f_cp_config_hash_hex"),
        _hex_ck("parameter_space_hash", "ck_l2f_cp_param_space_hex"),
        schema="experiments",
    )


def _create_plan_configs() -> None:
    op.create_table(
        "l2f_experiment_plan_configs",
        _uuid_pk(),
        _uuid("plan_id"),
        _uuid("config_payload_id"),
        _sha("config_hash"),
        _sha("parameter_space_hash"),
        sa.Column("config_index", sa.BigInteger(), nullable=False),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_l2f_experiment_plan_configs"),
        # the config's parameter_space_hash MUST equal the PLAN's parameter_space_hash.
        sa.ForeignKeyConstraint(
            ["plan_id", "parameter_space_hash"],
            [
                "experiments.l2f_experiment_plans.id",
                "experiments.l2f_experiment_plans.parameter_space_hash",
            ],
            name="fk_l2f_pc_plan_param_space",
        ),
        # config_hash AND parameter_space_hash MUST match the referenced payload (so a
        # payload from a different parameter space cannot be linked into the plan).
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
        # composite-UNIQUE target for the job FK.
        sa.UniqueConstraint("id", "plan_id", name="uq_l2f_pc_id_plan"),
        sa.CheckConstraint("config_index >= 0", name="ck_l2f_pc_config_index_nonneg"),
        _hex_ck("config_hash", "ck_l2f_pc_config_hash_hex"),
        _hex_ck("parameter_space_hash", "ck_l2f_pc_param_space_hex"),
        schema="experiments",
    )
    op.create_index(
        "ix_l2f_pc_plan_id", "l2f_experiment_plan_configs", ["plan_id"], schema="experiments"
    )


def _create_jobs() -> None:
    op.create_table(
        "l2f_experiment_jobs",
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
        # member + config MUST both belong to the job's plan (declarative).
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
            "status IN (" + ", ".join(f"'{s}'" for s in _JOB_STATUSES) + ")",
            name="ck_l2f_jobs_status_valid",
        ),
        _hex_ck("job_key", "ck_l2f_jobs_job_key_hex"),
        schema="experiments",
    )
    op.create_index(
        "ix_l2f_jobs_status_created_at",
        "l2f_experiment_jobs",
        ["status", "created_at"],
        schema="experiments",
    )


def _attach_append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_experiments_{table}_append_only "
        f"BEFORE UPDATE OR DELETE ON experiments.{table} "
        "FOR EACH ROW EXECUTE FUNCTION audit.minos_reject_mutation();"
    )


def upgrade() -> None:
    op.execute("SET ROLE minos_admin")
    _add_composite_targets()
    _create_plans()
    _create_plan_members()
    _create_config_payloads()
    _create_plan_configs()
    _create_jobs()

    # fully-immutable tables reuse the existing append-only function (0001).
    for table in (
        "l2f_experiment_plans",
        "l2f_experiment_plan_members",
        "l2f_config_payloads",
        "l2f_experiment_plan_configs",
    ):
        _attach_append_only(table)

    # jobs: scientific identity immutable, status/claim metadata mutable.
    conds = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _JOB_IDENTITY_COLS)
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_JOB_IDENTITY}() RETURNS trigger LANGUAGE plpgsql "
        "SET search_path = pg_catalog AS $$ "
        f"BEGIN IF {conds} THEN RAISE EXCEPTION "
        "'immutable identity: L2-F job scientific identity may not change'; END IF; "
        "RETURN NEW; END; $$;"
    )
    op.execute(
        "CREATE TRIGGER trg_l2f_jobs_identity_immutable "
        "BEFORE UPDATE ON experiments.l2f_experiment_jobs "
        f"FOR EACH ROW EXECUTE FUNCTION {_JOB_IDENTITY}();"
    )
    op.execute(
        "CREATE TRIGGER trg_l2f_jobs_no_delete "
        "BEFORE DELETE ON experiments.l2f_experiment_jobs "
        "FOR EACH ROW EXECUTE FUNCTION audit.minos_reject_mutation();"
    )

    # F3-A least privilege: owned by minos_admin; no grants to any application role.
    # F4 (claim/mutation for minos_runner) is unauthorized and grants nothing here.
    for table in _L2F_TABLES:
        op.execute(f"REVOKE ALL ON experiments.{table} FROM PUBLIC;")
        for role in ("minos_live", "minos_runner", "minos_trainer", "minos_evaluator"):
            op.execute(f"REVOKE ALL ON experiments.{table} FROM {role};")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    op.execute("DROP TRIGGER IF EXISTS trg_l2f_jobs_no_delete ON experiments.l2f_experiment_jobs;")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_l2f_jobs_identity_immutable ON experiments.l2f_experiment_jobs;"
    )
    for table in (
        "l2f_experiment_plans",
        "l2f_experiment_plan_members",
        "l2f_config_payloads",
        "l2f_experiment_plan_configs",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_experiments_{table}_append_only ON experiments.{table};"
        )
    for table in _L2F_TABLES:  # reverse dependency order
        op.drop_table(table, schema="experiments")
    op.execute(f"DROP FUNCTION IF EXISTS {_JOB_IDENTITY}();")
    _drop_composite_targets()
    op.execute("RESET ROLE")
