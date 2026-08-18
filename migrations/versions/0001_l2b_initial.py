"""L2-B initial storage foundation — immutable, self-contained schema snapshot.

Revision ID: 0001_l2b_initial
Revises:
Create Date: 2026-08-17

This revision is a FROZEN snapshot. It imports no ORM declarative base or model
metadata and never bulk-emits or reflects the ORM schema — every schema, role,
table, column, constraint, index, function, trigger, and grant is written explicitly
here, so replaying revision 0001 always produces the exact L2-B inventory regardless
of any later ORM changes (L2-C+).

Ownership model: the five NOLOGIN roles are created first; the seven schemas are
created ``AUTHORIZATION minos_admin``; all stage tables, functions, indexes, and
triggers are created while executing under ``SET ROLE minos_admin``, so
``minos_admin`` owns them and can administer (ALTER/DROP) them. ``minos_admin`` is a
NOLOGIN group/ownership role and is never a superuser. Unsafe ``PUBLIC`` privileges
(schemas, tables, sequences, function EXECUTE) are revoked and safe default
privileges are set; application roles receive only least-privilege grants and never
membership in ``minos_admin``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_l2b_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- frozen policy literals (independent of runtime constants) -------------------
_SCHEMAS: tuple[str, ...] = (
    "catalog",
    "profiling",
    "experiments",
    "evaluation",
    "models",
    "runtime",
    "audit",
)
_ROLES: tuple[str, ...] = (
    "minos_live",
    "minos_runner",
    "minos_evaluator",
    "minos_trainer",
    "minos_admin",
)
_HEX = "^[0-9a-f]{64}$"
_JOB_STATUSES = ("PENDING", "CLAIMED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
_APPEND_ONLY: tuple[tuple[str, str], ...] = (
    ("profiling", "profiles"),
    ("experiments", "results"),
    ("evaluation", "evaluations"),
    ("models", "model_bundles"),
    ("runtime", "decisions"),
    ("audit", "events"),
)
_REJECT_MUTATION = "audit.minos_reject_mutation"
_REJECT_IDENTITY = "experiments.minos_reject_identity_change"
_JOB_IDENTITY_COLS = ("id", "job_key", "profile_id", "config_id", "created_at")

# schema -> roles granted USAGE
_SCHEMA_USAGE: dict[str, tuple[str, ...]] = {
    "catalog": ("minos_live", "minos_runner", "minos_trainer"),
    "profiling": ("minos_live", "minos_runner", "minos_trainer"),
    "experiments": ("minos_runner", "minos_evaluator", "minos_trainer"),
    "evaluation": ("minos_evaluator",),
    "models": ("minos_live", "minos_trainer"),
    "runtime": ("minos_live",),
    "audit": ("minos_live", "minos_runner", "minos_evaluator", "minos_trainer"),
}
# (schema, table, role, privileges)
_TABLE_GRANTS: tuple[tuple[str, str, str, str], ...] = (
    ("catalog", "artifacts", "minos_live", "SELECT"),
    ("catalog", "artifacts", "minos_runner", "SELECT"),
    ("catalog", "artifacts", "minos_trainer", "SELECT"),
    ("catalog", "gatk_configs", "minos_live", "SELECT"),
    ("catalog", "gatk_configs", "minos_runner", "SELECT"),
    ("catalog", "gatk_configs", "minos_trainer", "SELECT"),
    ("catalog", "datasets", "minos_live", "SELECT"),
    ("catalog", "datasets", "minos_runner", "SELECT"),
    ("catalog", "datasets", "minos_trainer", "SELECT"),
    ("profiling", "profiles", "minos_live", "SELECT"),
    ("profiling", "profiles", "minos_runner", "SELECT, INSERT"),
    ("profiling", "profiles", "minos_trainer", "SELECT"),
    ("experiments", "jobs", "minos_runner", "SELECT, INSERT, UPDATE"),
    ("experiments", "results", "minos_runner", "SELECT, INSERT"),
    ("experiments", "results", "minos_evaluator", "SELECT"),
    ("experiments", "results", "minos_trainer", "SELECT"),
    ("evaluation", "evaluations", "minos_evaluator", "SELECT, INSERT"),
    ("models", "model_bundles", "minos_live", "SELECT"),
    ("models", "model_bundles", "minos_trainer", "SELECT, INSERT"),
    ("runtime", "decisions", "minos_live", "SELECT, INSERT"),
    ("audit", "events", "minos_live", "INSERT"),
    ("audit", "events", "minos_runner", "INSERT"),
    ("audit", "events", "minos_evaluator", "INSERT"),
    ("audit", "events", "minos_trainer", "INSERT"),
)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=False),
        nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.text("now()"),
    )


def _sha(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.CHAR(64), nullable=nullable)


def _hex_ck(col: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} ~ '{_HEX}'", name=name)


def _create_roles() -> None:
    for role in _ROLES:
        op.execute(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} NOLOGIN; END IF; END $$;"
        )


def _create_schemas() -> None:
    for schema in _SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION minos_admin;")


def _create_functions() -> None:
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_REJECT_MUTATION}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'append-only: % on %.% is not permitted', "
        "TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation'; END; $$;"
    )
    conds = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _JOB_IDENTITY_COLS)
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_REJECT_IDENTITY}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        f"BEGIN IF {conds} THEN RAISE EXCEPTION "
        "'immutable identity: identity columns of %.% cannot change', "
        "TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation'; END IF; "
        "RETURN NEW; END; $$;"
    )


def _create_tables() -> None:
    op.create_table(
        "artifacts",
        _uuid_pk(),
        sa.Column("uri", sa.Text(), nullable=False),
        _sha("sha256"),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("provenance", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.UniqueConstraint("sha256", name="uq_artifacts_sha256"),
        _hex_ck("sha256", "ck_artifacts_sha256_hex"),
        sa.CheckConstraint("length(uri) > 0", name="ck_artifacts_uri_nonempty"),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_artifacts_size_nonneg"
        ),
        schema="catalog",
    )
    op.create_table(
        "gatk_configs",
        _uuid_pk(),
        _sha("config_hash"),
        _sha("parameter_space_hash"),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_gatk_configs"),
        sa.UniqueConstraint("config_hash", name="uq_gatk_configs_config_hash"),
        _hex_ck("config_hash", "ck_gatk_configs_config_hash_hex"),
        _hex_ck("parameter_space_hash", "ck_gatk_configs_parameter_space_hash_hex"),
        schema="catalog",
    )
    op.create_table(
        "datasets",
        _uuid_pk(),
        sa.Column("dataset_id", sa.Text(), nullable=False),
        sa.Column("chromosome", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_datasets"),
        sa.UniqueConstraint("dataset_id", name="uq_datasets_dataset_id"),
        sa.CheckConstraint("length(dataset_id) > 0", name="ck_datasets_dataset_id_nonempty"),
        schema="catalog",
    )
    op.create_table(
        "profiles",
        _uuid_pk(),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=False), nullable=True),
        _sha("bam_sha256"),
        _sha("bai_sha256"),
        _sha("reference_sha256"),
        _sha("fai_sha256"),
        _sha("region_hash"),
        _sha("profile_manifest_hash"),
        _sha("fingerprint_hash"),
        _sha("identity_tuple_hash"),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_profiles"),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["catalog.datasets.id"], name="fk_profiles_dataset_id_datasets"
        ),
        sa.UniqueConstraint("profile_id", name="uq_profiles_profile_id"),
        sa.UniqueConstraint("identity_tuple_hash", name="uq_profiles_identity_tuple_hash"),
        sa.UniqueConstraint(
            "bam_sha256",
            "bai_sha256",
            "reference_sha256",
            "fai_sha256",
            "region_hash",
            name="uq_profiles_identity_tuple",
        ),
        sa.CheckConstraint("length(profile_id) > 0", name="ck_profiles_profile_id_nonempty"),
        _hex_ck("bam_sha256", "ck_profiles_bam_sha256_hex"),
        _hex_ck("bai_sha256", "ck_profiles_bai_sha256_hex"),
        _hex_ck("reference_sha256", "ck_profiles_reference_sha256_hex"),
        _hex_ck("fai_sha256", "ck_profiles_fai_sha256_hex"),
        _hex_ck("region_hash", "ck_profiles_region_hash_hex"),
        _hex_ck("profile_manifest_hash", "ck_profiles_profile_manifest_hash_hex"),
        _hex_ck("fingerprint_hash", "ck_profiles_fingerprint_hash_hex"),
        _hex_ck("identity_tuple_hash", "ck_profiles_identity_tuple_hash_hex"),
        schema="profiling",
    )
    op.create_table(
        "jobs",
        _uuid_pk(),
        sa.Column("job_key", sa.Text(), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("config_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        _ts("claimed_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiling.profiles.id"], name="fk_jobs_profile_id_profiles"
        ),
        sa.ForeignKeyConstraint(
            ["config_id"], ["catalog.gatk_configs.id"], name="fk_jobs_config_id_gatk_configs"
        ),
        sa.UniqueConstraint("job_key", name="uq_jobs_job_key"),
        sa.CheckConstraint("length(job_key) > 0", name="ck_jobs_job_key_nonempty"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _JOB_STATUSES) + ")",
            name="ck_jobs_status_valid",
        ),
        schema="experiments",
    )
    op.create_index(
        "ix_jobs_status_created_at", "jobs", ["status", "created_at"], schema="experiments"
    )
    op.create_table(
        "results",
        _uuid_pk(),
        sa.Column("job_id", postgresql.UUID(as_uuid=False), nullable=False),
        _sha("result_hash"),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_results"),
        sa.ForeignKeyConstraint(["job_id"], ["experiments.jobs.id"], name="fk_results_job_id_jobs"),
        sa.UniqueConstraint("job_id", name="uq_results_job_id"),
        sa.UniqueConstraint("result_hash", name="uq_results_result_hash"),
        _hex_ck("result_hash", "ck_results_result_hash_hex"),
        schema="experiments",
    )
    op.create_table(
        "evaluations",
        _uuid_pk(),
        sa.Column("experiment_result_id", postgresql.UUID(as_uuid=False), nullable=False),
        _sha("evaluation_hash"),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_evaluations"),
        sa.ForeignKeyConstraint(
            ["experiment_result_id"],
            ["experiments.results.id"],
            name="fk_evaluations_experiment_result_id_results",
        ),
        sa.UniqueConstraint("evaluation_hash", name="uq_evaluations_evaluation_hash"),
        _hex_ck("evaluation_hash", "ck_evaluations_evaluation_hash_hex"),
        schema="evaluation",
    )
    op.create_table(
        "model_bundles",
        _uuid_pk(),
        sa.Column("bundle_key", sa.Text(), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=False), nullable=False),
        _sha("feature_schema_hash", nullable=True),
        _sha("split_manifest_hash", nullable=True),
        _sha("training_provenance_hash", nullable=True),
        _sha("registry_hash", nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_model_bundles"),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["catalog.artifacts.id"], name="fk_model_bundles_artifact_id_artifacts"
        ),
        sa.UniqueConstraint("bundle_key", name="uq_model_bundles_bundle_key"),
        _hex_ck("feature_schema_hash", "ck_model_bundles_feature_schema_hash_hex"),
        _hex_ck("split_manifest_hash", "ck_model_bundles_split_manifest_hash_hex"),
        _hex_ck("training_provenance_hash", "ck_model_bundles_training_provenance_hash_hex"),
        _hex_ck("registry_hash", "ck_model_bundles_registry_hash_hex"),
        schema="models",
    )
    op.create_table(
        "decisions",
        _uuid_pk(),
        sa.Column("round_id", sa.Text(), nullable=False),
        _sha("decision_hash"),
        _sha("decision_manifest_hash"),
        sa.Column("config_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("model_bundle_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=False), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_decisions"),
        sa.ForeignKeyConstraint(
            ["config_id"], ["catalog.gatk_configs.id"], name="fk_decisions_config_id_gatk_configs"
        ),
        sa.ForeignKeyConstraint(
            ["model_bundle_id"],
            ["models.model_bundles.id"],
            name="fk_decisions_model_bundle_id_model_bundles",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiling.profiles.id"], name="fk_decisions_profile_id_profiles"
        ),
        sa.UniqueConstraint(
            "round_id", "decision_hash", name="uq_decisions_round_id_decision_hash"
        ),
        sa.CheckConstraint("length(round_id) > 0", name="ck_decisions_round_id_nonempty"),
        _hex_ck("decision_hash", "ck_decisions_decision_hash_hex"),
        _hex_ck("decision_manifest_hash", "ck_decisions_decision_manifest_hash_hex"),
        schema="runtime",
    )
    op.create_table(
        "events",
        _uuid_pk(),
        sa.Column("actor_role", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_kind", sa.Text(), nullable=True),
        sa.Column("object_id", sa.Text(), nullable=True),
        _sha("payload_hash"),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.CheckConstraint("length(actor_role) > 0", name="ck_events_actor_role_nonempty"),
        sa.CheckConstraint("length(action) > 0", name="ck_events_action_nonempty"),
        _hex_ck("payload_hash", "ck_events_payload_hash_hex"),
        schema="audit",
    )


def _create_triggers() -> None:
    for schema, table in _APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER trg_{schema}_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {schema}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
        )
    op.execute(
        "CREATE TRIGGER trg_experiments_jobs_identity_immutable "
        "BEFORE UPDATE ON experiments.jobs "
        f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_IDENTITY}();"
    )


def _revoke_and_grant() -> None:
    for schema in _SCHEMAS:
        op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;")
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC;")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM PUBLIC;")
    op.execute(f"REVOKE ALL ON FUNCTION {_REJECT_MUTATION}() FROM PUBLIC;")
    op.execute(f"REVOKE ALL ON FUNCTION {_REJECT_IDENTITY}() FROM PUBLIC;")
    for schema, roles in _SCHEMA_USAGE.items():
        for role in roles:
            op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role};")
    for schema, table, role, privs in _TABLE_GRANTS:
        op.execute(f"GRANT {privs} ON {schema}.{table} TO {role};")


def _default_privileges(*, install: bool) -> None:
    # Safe defaults for objects created later while operating as minos_admin: PUBLIC
    # gets nothing on tables/sequences and no EXECUTE on functions. On downgrade the
    # inverse restores built-in defaults so pg_default_acl entries are removed and the
    # role can be dropped cleanly.
    verb_tables = "REVOKE ALL ON TABLES FROM PUBLIC" if install else "GRANT ALL ON TABLES TO PUBLIC"
    verb_seq = (
        "REVOKE ALL ON SEQUENCES FROM PUBLIC" if install else "GRANT ALL ON SEQUENCES TO PUBLIC"
    )
    verb_func = (
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        if install
        else "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
    )
    for schema in _SCHEMAS:
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} {verb_func};")
        if install:
            op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} {verb_tables};")
            op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} {verb_seq};")
        else:
            # Only functions deviate from built-in defaults; restore that one entry.
            pass


def upgrade() -> None:
    _create_roles()
    _create_schemas()
    op.execute("SET ROLE minos_admin")
    _create_functions()
    _create_tables()
    _create_triggers()
    _revoke_and_grant()
    _default_privileges(install=True)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    # triggers
    for schema, table in _APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{schema}_{table}_append_only ON {schema}.{table};")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_experiments_jobs_identity_immutable ON experiments.jobs;"
    )
    _default_privileges(install=False)
    # Revoke grants from the four application roles only. minos_admin is the OWNER of
    # the schemas/tables/functions — its access is by ownership, not grants, so it must
    # NOT be revoked (revoking its schema USAGE would break the drops below).
    _app_roles = tuple(r for r in _ROLES if r != "minos_admin")
    for schema in _SCHEMAS:
        for role in _app_roles:
            op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role};")
            op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role};")
            op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {role};")
    # tables (reverse dependency order)
    op.drop_table("events", schema="audit")
    op.drop_table("decisions", schema="runtime")
    op.drop_table("model_bundles", schema="models")
    op.drop_table("evaluations", schema="evaluation")
    op.drop_table("results", schema="experiments")
    op.drop_index("ix_jobs_status_created_at", table_name="jobs", schema="experiments")
    op.drop_table("jobs", schema="experiments")
    op.drop_table("profiles", schema="profiling")
    op.drop_table("datasets", schema="catalog")
    op.drop_table("gatk_configs", schema="catalog")
    op.drop_table("artifacts", schema="catalog")
    op.execute(f"DROP FUNCTION IF EXISTS {_REJECT_IDENTITY}();")
    op.execute(f"DROP FUNCTION IF EXISTS {_REJECT_MUTATION}();")
    for schema in _SCHEMAS:
        op.execute(f"DROP SCHEMA IF EXISTS {schema} RESTRICT;")
    op.execute("RESET ROLE")
    # roles are cluster-global: drop exactly the five, retained safely (with a NOTICE)
    # if still referenced by another database — never a destructive database-wide op.
    for role in _ROLES:
        op.execute(
            "DO $$ BEGIN "
            f"DROP ROLE IF EXISTS {role}; "
            "EXCEPTION WHEN dependent_objects_still_exist THEN "
            f"RAISE NOTICE 'role {role} retained: still referenced in another database'; "
            "END $$;"
        )
