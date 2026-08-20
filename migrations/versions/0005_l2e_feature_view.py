"""L2-E production feature view — feature sets, logical matrices, partition boundary.

Revision ID: 0005_l2e_feature_view
Revises: 0004_l2d_profile_ingestion
Create Date: 2026-08-20

Additive, immutable, self-contained snapshot (like 0001..0004): no ORM base import, no
mutable runtime schema inventory — every object written explicitly. Continues the SINGLE
Alembic lineage on top of the accepted L2-D profile-ingestion head.

Authority model (frozen in docs/layer2/FEATURE_VIEW.md):
  * ``profiling.feature_sets`` — the frozen FEATURE-READY column manifests
    (``feature_set_hash`` UNIQUE; hashes + JSONB manifest; append-only).
  * ``profiling.feature_matrices`` — LOGICAL matrix identity rows: FK to the frozen
    profile snapshot, ``partition`` CHECK IN ('train','validation') — a test matrix is
    STRUCTURALLY impossible — FK to the feature set, ``matrix_hash`` UNIQUE (logical),
    ``artifact_sha256`` + FK to ``catalog.artifacts`` (exact Parquet bytes, kind
    ``l2e:feature-matrix-parquet``; never conflated with matrix_hash),
    UNIQUE(profile_snapshot_id, partition, feature_set_id) = logical identity.
    Append-only.
  * ``profiling.feature_matrix_members`` — membership rows carry HASHES ONLY
    (vector_hash, feature_values_hash) — plaintext feature values are never stored in
    the database. UNIQUE(matrix, dataset) + UNIQUE(matrix, member_index). Append-only.
  * Security-barrier partition views: ``profiling.training_matrix`` → ``minos_trainer``
    only; ``evaluation.validation_matrix`` → ``minos_evaluator`` only. No test-matrix
    table, view, row, function, or grant exists. Base tables carry NO application-role
    privileges; writes run as the schema owner (``minos_admin``).

Privilege delta (stage-specific; the accepted 0001 migration, storage/roles.py and its
frozen role_policy_hash are NOT modified):
  * 0001 granted ``catalog.artifacts`` SELECT to minos_live, minos_runner and
    minos_trainer. minos_evaluator has neither that grant nor catalog schema USAGE.
  * Upgrade REVOKES ``catalog.artifacts`` SELECT from minos_trainer and (explicitly,
    as a no-op guard) from minos_evaluator: artifact references become reachable only
    through the caller's partition view.
  * Downgrade restores SELECT ONLY to minos_trainer — exactly the 0001 state. It never
    grants minos_evaluator catalog access or artifact SELECT it never had.
  * minos_live / minos_runner artifact access is untouched in both directions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_l2e_feature_view"
down_revision: str | None = "0004_l2d_profile_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX = "^[0-9a-f]{64}$"
_REJECT_MUTATION = "audit.minos_reject_mutation"
_APP_ROLES = ("minos_live", "minos_runner", "minos_trainer", "minos_evaluator")

_TABLES = ("feature_sets", "feature_matrices", "feature_matrix_members")

# Member-level partition projection: matrix metadata + membership + the artifact
# reference for the caller's OWN partition. No JSONB manifests, no base-table access.
_MATRIX_VIEW_COLUMNS = (
    "ps.epoch",
    "ps.snapshot_hash",
    "fm.partition",
    "fs.feature_set_hash",
    "fs.registry_hash",
    "fm.matrix_hash",
    "fm.artifact_sha256",
    "art.uri AS artifact_uri",
    "fm.row_count",
    "fm.column_count",
    "dr.dataset_id",
    "mm.member_index",
    "mm.vector_hash",
    "mm.feature_values_hash",
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


def _ts() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _sha(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.CHAR(64), nullable=nullable)


def _hex_ck(col: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} ~ '{_HEX}'", name=name)


def _create_feature_sets() -> None:
    op.create_table(
        "feature_sets",
        _uuid_pk(),
        _sha("feature_set_hash"),
        _sha("registry_hash"),
        sa.Column("column_count", sa.BigInteger(), nullable=False),
        sa.Column("column_manifest", postgresql.JSONB(), nullable=False),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_feature_sets"),
        sa.UniqueConstraint("feature_set_hash", name="uq_feature_sets_feature_set_hash"),
        sa.CheckConstraint("column_count > 0", name="ck_feature_sets_columns_positive"),
        _hex_ck("feature_set_hash", "ck_feature_sets_set_hash_hex"),
        _hex_ck("registry_hash", "ck_feature_sets_registry_hash_hex"),
        schema="profiling",
    )


def _create_feature_matrices() -> None:
    op.create_table(
        "feature_matrices",
        _uuid_pk(),
        _uuid("profile_snapshot_id"),
        sa.Column("partition", sa.Text(), nullable=False),
        _uuid("feature_set_id"),
        _sha("matrix_hash"),
        _sha("artifact_sha256"),
        _uuid("matrix_artifact_id"),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("column_count", sa.BigInteger(), nullable=False),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_feature_matrices"),
        sa.ForeignKeyConstraint(
            ["profile_snapshot_id"],
            ["profiling.profile_snapshots.id"],
            name="fk_feature_matrices_profile_snapshot_id",
        ),
        sa.ForeignKeyConstraint(
            ["feature_set_id"],
            ["profiling.feature_sets.id"],
            name="fk_feature_matrices_feature_set_id",
        ),
        sa.ForeignKeyConstraint(
            ["matrix_artifact_id"],
            ["catalog.artifacts.id"],
            name="fk_feature_matrices_matrix_artifact_id_artifacts",
        ),
        sa.UniqueConstraint("matrix_hash", name="uq_feature_matrices_matrix_hash"),
        # the LOGICAL matrix identity (frozen): one matrix per snapshot+partition+set.
        sa.UniqueConstraint(
            "profile_snapshot_id",
            "partition",
            "feature_set_id",
            name="uq_feature_matrices_logical_identity",
        ),
        # a test matrix is STRUCTURALLY impossible.
        sa.CheckConstraint(
            "partition IN ('train', 'validation')", name="ck_feature_matrices_partition_valid"
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_feature_matrices_rows_nonneg"),
        sa.CheckConstraint("column_count > 0", name="ck_feature_matrices_columns_positive"),
        _hex_ck("matrix_hash", "ck_feature_matrices_matrix_hash_hex"),
        _hex_ck("artifact_sha256", "ck_feature_matrices_artifact_sha_hex"),
        schema="profiling",
    )
    op.create_index(
        "ix_feature_matrices_profile_snapshot_id",
        "feature_matrices",
        ["profile_snapshot_id"],
        schema="profiling",
    )


def _create_feature_matrix_members() -> None:
    op.create_table(
        "feature_matrix_members",
        _uuid_pk(),
        _uuid("feature_matrix_id"),
        _uuid("dataset_registry_id"),
        sa.Column("member_index", sa.BigInteger(), nullable=False),
        _sha("vector_hash"),
        _sha("feature_values_hash"),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_feature_matrix_members"),
        sa.ForeignKeyConstraint(
            ["feature_matrix_id"],
            ["profiling.feature_matrices.id"],
            name="fk_feature_matrix_members_matrix_id",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_feature_matrix_members_dataset_registry_id",
        ),
        sa.UniqueConstraint(
            "feature_matrix_id",
            "dataset_registry_id",
            name="uq_feature_matrix_members_matrix_dataset",
        ),
        sa.UniqueConstraint(
            "feature_matrix_id",
            "member_index",
            name="uq_feature_matrix_members_matrix_index",
        ),
        sa.CheckConstraint("member_index >= 0", name="ck_feature_matrix_members_index_nonneg"),
        _hex_ck("vector_hash", "ck_feature_matrix_members_vector_hex"),
        _hex_ck("feature_values_hash", "ck_feature_matrix_members_feature_hex"),
        schema="profiling",
    )
    op.create_index(
        "ix_feature_matrix_members_matrix_id",
        "feature_matrix_members",
        ["feature_matrix_id"],
        schema="profiling",
    )


def _create_views() -> None:
    cols = ", ".join(_MATRIX_VIEW_COLUMNS)
    # Start FROM feature_matrices so a zero-row matrix still exposes ONE matrix-level
    # row (its artifact reference) with NULL member columns. Members and their registry
    # rows are LEFT JOINed; the artifact/set/snapshot are always present (INNER).
    join = (
        "FROM profiling.feature_matrices fm "
        "JOIN profiling.feature_sets fs ON fs.id = fm.feature_set_id "
        "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
        "JOIN catalog.artifacts art ON art.id = fm.matrix_artifact_id "
        "LEFT JOIN profiling.feature_matrix_members mm ON mm.feature_matrix_id = fm.id "
        "LEFT JOIN catalog.dataset_registry dr ON dr.id = mm.dataset_registry_id "
    )
    op.execute(
        f"CREATE VIEW profiling.training_matrix WITH (security_barrier = true) AS "
        f"SELECT {cols} {join} WHERE fm.partition = 'train';"
    )
    op.execute(
        f"CREATE VIEW evaluation.validation_matrix WITH (security_barrier = true) AS "
        f"SELECT {cols} {join} WHERE fm.partition = 'validation';"
    )
    # deliberately NO test-matrix view, function, row source, or grant of any kind.


def _create_triggers() -> None:
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER trg_profiling_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON profiling.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
        )


def _grant() -> None:
    for table in _TABLES:
        op.execute(f"REVOKE ALL ON profiling.{table} FROM PUBLIC;")
        for role in _APP_ROLES:
            op.execute(f"REVOKE ALL ON profiling.{table} FROM {role};")
    for view in ("profiling.training_matrix", "evaluation.validation_matrix"):
        op.execute(f"REVOKE ALL ON {view} FROM PUBLIC;")
    op.execute("GRANT SELECT ON profiling.training_matrix TO minos_trainer;")
    op.execute("GRANT SELECT ON evaluation.validation_matrix TO minos_evaluator;")
    # Stage-specific privilege delta (E3 ruling): unrestricted catalog.artifacts reads
    # end here — artifact references are reachable only through the partition views.
    # minos_evaluator never had this grant; the explicit revoke is a no-op guard.
    op.execute("REVOKE SELECT ON catalog.artifacts FROM minos_trainer;")
    op.execute("REVOKE SELECT ON catalog.artifacts FROM minos_evaluator;")
    # minos_live / minos_runner legacy artifact access is deliberately untouched.


def upgrade() -> None:
    op.execute("SET ROLE minos_admin")
    _create_feature_sets()
    _create_feature_matrices()
    _create_feature_matrix_members()
    _create_views()
    _create_triggers()
    _grant()
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    for table in _TABLES:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_profiling_{table}_append_only ON profiling.{table};"
        )
    op.execute("DROP VIEW IF EXISTS profiling.training_matrix;")
    op.execute("DROP VIEW IF EXISTS evaluation.validation_matrix;")
    op.drop_index(
        "ix_feature_matrix_members_matrix_id",
        table_name="feature_matrix_members",
        schema="profiling",
    )
    op.drop_table("feature_matrix_members", schema="profiling")
    op.drop_index(
        "ix_feature_matrices_profile_snapshot_id",
        table_name="feature_matrices",
        schema="profiling",
    )
    op.drop_table("feature_matrices", schema="profiling")
    op.drop_table("feature_sets", schema="profiling")
    # Restore EXACTLY the 0001 privilege state: SELECT back to minos_trainer ONLY.
    # minos_evaluator never had catalog.artifacts SELECT or catalog USAGE — the
    # downgrade must not (and does not) grant it anything.
    op.execute("GRANT SELECT ON catalog.artifacts TO minos_trainer;")
    op.execute("RESET ROLE")
