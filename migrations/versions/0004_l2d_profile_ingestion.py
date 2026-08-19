"""L2-D Layer 1 profile ingestion — epoch-bound, additive, immutable snapshot.

Revision ID: 0004_l2d_profile_ingestion
Revises: 0003_l2c_split_v2_epochs
Create Date: 2026-08-19

Additive, immutable, self-contained snapshot (like 0001/0002/0003): no ORM base import,
every object written explicitly. Continues the SINGLE Alembic lineage on top of the
accepted SPLIT-FROZEN-V2 epoch registry — the epoch tables are a hard dependency
(``profile_snapshots`` FKs ``catalog.split_snapshots``).

Authority model (owner ruling):
  * ``profiling.bam_profiles`` — canonical immutable CONTENT authority: one row per
    accepted COMPLETE profile version. ``feature_values_hash`` (frozen canonical
    algorithm) is NOT NULL; ``m5_status`` may only be MATCH or ABSENT (MISMATCH is never
    admitted) with ``integrity_degraded`` exactly when ABSENT; ``content_hash`` is the
    location-independent version key (UNIQUE).
  * ``profiling.profile_ingest_attempts`` — operational attempt log (ADMITTED/REJECTED
    with reasons). FAILED/PARTIAL attempts live here and never in the accepted corpus.
  * ``profiling.profile_snapshots`` + ``profiling.profile_snapshot_members`` — canonical
    EPOCH membership: a snapshot binds one split epoch (FK + split manifest hash +
    registry snapshot hash) and selects the exact accepted ``bam_profiles`` version per
    identity. Downstream (L2-E) consumes ONLY through the partition views below, and only
    from PROFILE-SNAPSHOT-FROZEN-<epoch> PASS snapshots.
  * Views hide raw JSONB, artifact ids/URIs, identity hashes, and coordinates:
      - ``profiling.training_profile_members`` (train) → ``minos_trainer``;
      - ``evaluation.validation_profile_members`` (validation) → ``minos_evaluator``;
      - ``evaluation.sealed_test_profile_members`` (test) → granted to NO role until a
        separately authorized final-evaluation migration.
  * Legacy ``profiling.profiles`` becomes compatibility-only: SELECT is revoked from
    ``minos_trainer``/``minos_live`` (owner ruling: no downstream reads); restored
    exactly on downgrade. ``storage/roles.py`` is untouched (role_policy_hash frozen).

Ingestion writes run as the schema owner (``minos_admin``) through
``storage.profile_ingest`` — the intake producer holds no write path, and no application
role has INSERT on any new table. All new tables are append-only
(``audit.minos_reject_mutation``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_l2d_profile_ingestion"
down_revision: str | None = "0003_l2c_split_v2_epochs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX = "^[0-9a-f]{64}$"
_PARTITIONS = ("train", "validation", "test")
_REJECT_MUTATION = "audit.minos_reject_mutation"

#: Legacy L2-B reads revoked by L2-D (owner ruling: profiles is compatibility-only,
#: no downstream reads). Restored exactly on downgrade. minos_runner keeps its grant
#: (the legacy write path is unchanged; roles.py is not modified).
_REVOKE_LEGACY: tuple[tuple[str, str], ...] = (
    ("profiling.profiles", "minos_trainer"),
    ("profiling.profiles", "minos_live"),
)

# Leakage-safe member projection: membership + integrity identity only. No JSONB, no
# artifact ids/URIs, no bam/bai/reference/fai hashes, no region coordinates.
_MEMBER_VIEW_COLUMNS = (
    "dr.dataset_id",
    "ps.epoch",
    "ps.snapshot_hash",
    "ps.split_manifest_hash",
    "ps.registry_snapshot_hash",
    "m.partition",
    "m.feature_values_hash",
    "bp.profiler_version",
    "bp.profiler_config_hash",
    "bp.windows_row_count",
    "bp.integrity_degraded",
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


def _hex_ck(col: str, name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} ~ '{_HEX}'"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=name)


def _create_bam_profiles() -> None:
    op.create_table(
        "bam_profiles",
        _uuid_pk(),
        _uuid("dataset_registry_id"),
        sa.Column("profile_id", sa.Text(), nullable=False),
        # registered identity mirror (cross-checked against catalog.dataset_registry)
        _sha("bam_sha256"),
        _sha("bai_sha256"),
        _sha("reference_sha256"),
        _sha("fai_sha256"),
        _sha("region_hash"),
        _sha("identity_tuple_hash"),
        # input-integrity attestation (intake-produced; MISMATCH never admitted)
        sa.Column("m5_status", sa.Text(), nullable=False),
        sa.Column("integrity_degraded", sa.Boolean(), nullable=False),
        _sha("attestation_hash"),
        _sha("registry_snapshot_hash"),
        # admission facts (COMPLETE only)
        sa.Column("profile_status", sa.Text(), nullable=False),
        sa.Column("profiler_version", sa.Text(), nullable=False),
        _sha("profiler_config_hash"),
        sa.Column("windows_row_count", sa.BigInteger(), nullable=False),
        # frozen canonical feature-values hash (owner ruling) + L1 cross-binding
        _sha("feature_values_hash"),
        _sha("l1_feature_values_hash"),
        sa.Column("eligible_value_count", sa.BigInteger(), nullable=False),
        # complete profile document + exact artifact byte hashes + artifact references
        sa.Column("profile_document", postgresql.JSONB(), nullable=False),
        _sha("profile_sha256"),
        _sha("profile_manifest_sha256"),
        _sha("windows_sha256"),
        _uuid("profile_artifact_id"),
        _uuid("profile_manifest_artifact_id"),
        _uuid("windows_artifact_id"),
        # canonical ingestion identity (idempotency/conflict key) + content version key
        _sha("ingestion_key"),
        _sha("content_hash"),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_bam_profiles"),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_bam_profiles_dataset_registry_id",
        ),
        sa.ForeignKeyConstraint(
            ["profile_artifact_id"],
            ["catalog.artifacts.id"],
            name="fk_bam_profiles_profile_artifact_id_artifacts",
        ),
        sa.ForeignKeyConstraint(
            ["windows_artifact_id"],
            ["catalog.artifacts.id"],
            name="fk_bam_profiles_windows_artifact_id_artifacts",
        ),
        sa.ForeignKeyConstraint(
            ["profile_manifest_artifact_id"],
            ["catalog.artifacts.id"],
            name="fk_bam_profiles_manifest_artifact_id_artifacts",
        ),
        sa.UniqueConstraint("content_hash", name="uq_bam_profiles_content_hash"),
        sa.UniqueConstraint("ingestion_key", name="uq_bam_profiles_ingestion_key"),
        # concurrency-safe profile-id conflict enforcement (application SELECT is raceable)
        sa.UniqueConstraint("profile_id", name="uq_bam_profiles_profile_id"),
        sa.CheckConstraint(
            "m5_status IN ('MATCH', 'ABSENT')", name="ck_bam_profiles_m5_admissible"
        ),
        sa.CheckConstraint(
            "(m5_status = 'MATCH' AND NOT integrity_degraded) "
            "OR (m5_status = 'ABSENT' AND integrity_degraded)",
            name="ck_bam_profiles_degraded_iff_absent",
        ),
        sa.CheckConstraint("profile_status = 'COMPLETE'", name="ck_bam_profiles_complete_only"),
        sa.CheckConstraint("windows_row_count > 0", name="ck_bam_profiles_windows_positive"),
        sa.CheckConstraint("eligible_value_count > 0", name="ck_bam_profiles_eligible_positive"),
        _hex_ck("bam_sha256", "ck_bam_profiles_bam_hex"),
        _hex_ck("bai_sha256", "ck_bam_profiles_bai_hex"),
        _hex_ck("reference_sha256", "ck_bam_profiles_reference_hex"),
        _hex_ck("fai_sha256", "ck_bam_profiles_fai_hex"),
        _hex_ck("region_hash", "ck_bam_profiles_region_hex"),
        _hex_ck("identity_tuple_hash", "ck_bam_profiles_identity_hex"),
        _hex_ck("attestation_hash", "ck_bam_profiles_attestation_hex"),
        _hex_ck("registry_snapshot_hash", "ck_bam_profiles_registry_snapshot_hex"),
        _hex_ck("feature_values_hash", "ck_bam_profiles_feature_values_hex"),
        _hex_ck("l1_feature_values_hash", "ck_bam_profiles_l1_feature_values_hex"),
        _hex_ck("profiler_config_hash", "ck_bam_profiles_profiler_config_hex"),
        _hex_ck("profile_sha256", "ck_bam_profiles_profile_sha_hex"),
        _hex_ck("profile_manifest_sha256", "ck_bam_profiles_manifest_sha_hex"),
        _hex_ck("ingestion_key", "ck_bam_profiles_ingestion_key_hex"),
        _hex_ck("windows_sha256", "ck_bam_profiles_windows_sha_hex"),
        _hex_ck("content_hash", "ck_bam_profiles_content_hex"),
        schema="profiling",
    )
    op.create_index(
        "ix_bam_profiles_dataset_registry_id",
        "bam_profiles",
        ["dataset_registry_id"],
        schema="profiling",
    )
    op.create_index(
        "ix_bam_profiles_identity_tuple_hash",
        "bam_profiles",
        ["identity_tuple_hash"],
        schema="profiling",
    )


def _create_ingest_attempts() -> None:
    op.create_table(
        "profile_ingest_attempts",
        _uuid_pk(),
        _uuid("dataset_registry_id", nullable=True),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        _sha("attestation_hash", nullable=True),
        _sha("content_hash", nullable=True),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_profile_ingest_attempts"),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_profile_ingest_attempts_dataset_registry_id",
        ),
        sa.CheckConstraint(
            "outcome IN ('ADMITTED', 'REJECTED')", name="ck_profile_ingest_attempts_outcome"
        ),
        _hex_ck("attestation_hash", "ck_profile_ingest_attempts_attestation_hex", nullable=True),
        _hex_ck("content_hash", "ck_profile_ingest_attempts_content_hex", nullable=True),
        schema="profiling",
    )


def _create_profile_snapshots() -> None:
    op.create_table(
        "profile_snapshots",
        _uuid_pk(),
        sa.Column("epoch", sa.Integer(), nullable=False),
        _uuid("split_snapshot_id"),
        _sha("split_manifest_hash"),
        _sha("registry_snapshot_hash"),
        sa.Column("member_count", sa.BigInteger(), nullable=False),
        _sha("snapshot_hash"),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_profile_snapshots"),
        sa.ForeignKeyConstraint(
            ["split_snapshot_id"],
            ["catalog.split_snapshots.id"],
            name="fk_profile_snapshots_split_snapshot_id_split_snapshots",
        ),
        sa.UniqueConstraint("epoch", name="uq_profile_snapshots_epoch"),
        sa.UniqueConstraint("split_snapshot_id", name="uq_profile_snapshots_split_snapshot"),
        sa.UniqueConstraint("snapshot_hash", name="uq_profile_snapshots_snapshot_hash"),
        sa.CheckConstraint("epoch >= 1", name="ck_profile_snapshots_epoch_positive"),
        sa.CheckConstraint("member_count > 0", name="ck_profile_snapshots_members_positive"),
        _hex_ck("split_manifest_hash", "ck_profile_snapshots_split_manifest_hex"),
        _hex_ck("registry_snapshot_hash", "ck_profile_snapshots_registry_hex"),
        _hex_ck("snapshot_hash", "ck_profile_snapshots_snapshot_hex"),
        schema="profiling",
    )


def _create_snapshot_members() -> None:
    op.create_table(
        "profile_snapshot_members",
        _uuid_pk(),
        _uuid("profile_snapshot_id"),
        _uuid("bam_profile_id"),
        _uuid("dataset_registry_id"),
        sa.Column("partition", sa.Text(), nullable=False),
        _sha("feature_values_hash"),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_profile_snapshot_members"),
        sa.ForeignKeyConstraint(
            ["profile_snapshot_id"],
            ["profiling.profile_snapshots.id"],
            name="fk_profile_snapshot_members_snapshot_id",
        ),
        sa.ForeignKeyConstraint(
            ["bam_profile_id"],
            ["profiling.bam_profiles.id"],
            name="fk_profile_snapshot_members_bam_profile_id",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_profile_snapshot_members_dataset_registry_id",
        ),
        # exactly one accepted profile version per identity per epoch snapshot.
        sa.UniqueConstraint(
            "profile_snapshot_id",
            "dataset_registry_id",
            name="uq_profile_snapshot_members_snapshot_dataset",
        ),
        sa.CheckConstraint(
            "partition IN (" + ", ".join(f"'{p}'" for p in _PARTITIONS) + ")",
            name="ck_profile_snapshot_members_partition_valid",
        ),
        _hex_ck("feature_values_hash", "ck_profile_snapshot_members_feature_hex"),
        schema="profiling",
    )
    op.create_index(
        "ix_profile_snapshot_members_snapshot_id",
        "profile_snapshot_members",
        ["profile_snapshot_id"],
        schema="profiling",
    )


def _create_views() -> None:
    cols = ", ".join(_MEMBER_VIEW_COLUMNS)
    join = (
        "FROM profiling.profile_snapshot_members m "
        "JOIN profiling.profile_snapshots ps ON ps.id = m.profile_snapshot_id "
        "JOIN profiling.bam_profiles bp ON bp.id = m.bam_profile_id "
        "JOIN catalog.dataset_registry dr ON dr.id = m.dataset_registry_id "
    )
    op.execute(
        f"CREATE VIEW profiling.training_profile_members AS "
        f"SELECT {cols} {join} WHERE m.partition = 'train';"
    )
    op.execute(
        f"CREATE VIEW evaluation.validation_profile_members AS "
        f"SELECT {cols} {join} WHERE m.partition = 'validation';"
    )
    # Sealed test membership: exists, granted to NO role until final-evaluation
    # authorization (mirrors evaluation.sealed_test_epoch_allocations).
    op.execute(
        f"CREATE VIEW evaluation.sealed_test_profile_members AS "
        f"SELECT {cols} {join} WHERE m.partition = 'test';"
    )


def _create_triggers() -> None:
    for table in (
        "bam_profiles",
        "profile_ingest_attempts",
        "profile_snapshots",
        "profile_snapshot_members",
    ):
        op.execute(
            f"CREATE TRIGGER trg_profiling_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON profiling.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
        )


def _grant() -> None:
    for obj in (
        "profiling.bam_profiles",
        "profiling.profile_ingest_attempts",
        "profiling.profile_snapshots",
        "profiling.profile_snapshot_members",
        "profiling.training_profile_members",
        "evaluation.validation_profile_members",
        "evaluation.sealed_test_profile_members",
    ):
        op.execute(f"REVOKE ALL ON {obj} FROM PUBLIC;")
    op.execute("GRANT SELECT ON profiling.training_profile_members TO minos_trainer;")
    op.execute("GRANT SELECT ON evaluation.validation_profile_members TO minos_evaluator;")
    # evaluation.sealed_test_profile_members: NO GRANT — denied until authorized.
    # Legacy profiles become compatibility-only (owner ruling); restored on downgrade.
    for table, role in _REVOKE_LEGACY:
        op.execute(f"REVOKE SELECT ON {table} FROM {role};")


def upgrade() -> None:
    op.execute("SET ROLE minos_admin")
    _create_bam_profiles()
    _create_ingest_attempts()
    _create_profile_snapshots()
    _create_snapshot_members()
    _create_views()
    _create_triggers()
    _grant()
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    for table in (
        "bam_profiles",
        "profile_ingest_attempts",
        "profile_snapshots",
        "profile_snapshot_members",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_profiling_{table}_append_only ON profiling.{table};"
        )
    op.execute("DROP VIEW IF EXISTS profiling.training_profile_members;")
    op.execute("DROP VIEW IF EXISTS evaluation.validation_profile_members;")
    op.execute("DROP VIEW IF EXISTS evaluation.sealed_test_profile_members;")
    op.drop_index(
        "ix_profile_snapshot_members_snapshot_id",
        table_name="profile_snapshot_members",
        schema="profiling",
    )
    op.drop_table("profile_snapshot_members", schema="profiling")
    op.drop_table("profile_snapshots", schema="profiling")
    op.drop_index(
        "ix_bam_profiles_identity_tuple_hash", table_name="bam_profiles", schema="profiling"
    )
    op.drop_index(
        "ix_bam_profiles_dataset_registry_id", table_name="bam_profiles", schema="profiling"
    )
    op.drop_table("profile_ingest_attempts", schema="profiling")
    op.drop_table("bam_profiles", schema="profiling")
    # restore the legacy L2-B reads exactly as 0001 established them.
    for table, role in _REVOKE_LEGACY:
        op.execute(f"GRANT SELECT ON {table} TO {role};")
    op.execute("RESET ROLE")
