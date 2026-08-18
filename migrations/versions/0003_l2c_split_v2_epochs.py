"""L2-C SPLIT-FROZEN v2 — epoched, growth-stable dataset split registry.

Revision ID: 0003_l2c_split_v2_epochs
Revises: 0002_l2c_dataset_split
Create Date: 2026-08-18

Additive, immutable, self-contained snapshot (like 0001/0002): it imports no ORM base
and writes every object explicitly. It supersedes the v1 fixed-count split (which stays
frozen as historical policy-v1) with a growth-stable, stratified, **epoched** split. Each
epoch is an immutable frozen snapshot that is a superset of its parent; existing
allocations are never re-labelled (the test set is monotonic). Epoch 1 inherits the
accepted v1 partitions verbatim.

Objects (all owned by ``minos_admin``; append-only; leakage-safe views only):
  * ``catalog.split_snapshots`` — one immutable row per epoch, with the growth-capable
    ``registry_snapshot_hash``, an explicit ``parent_snapshot_id`` self-FK + parent
    manifest/registry hashes, and a ``transition_count`` (0 for a valid epoch).
  * ``catalog.split_epoch_allocations`` — one row per (epoch, registered identity) with
    its partition, ``origin_epoch`` and ``assignment_source``.
  * Access is partition-separated:
      - ``catalog.training_epoch_allocations`` (train) → ``minos_trainer``;
      - ``evaluation.validation_epoch_allocations`` (validation) → ``minos_evaluator``;
      - ``evaluation.sealed_test_epoch_allocations`` (test) → **granted to no role**;
        the sealed test cohort stays unreadable until a separate, explicitly authorized
        final-evaluation migration grants access.
The reused ``catalog.dataset_registry`` identities and the frozen v1 ``split_allocations``
are untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_l2c_split_v2_epochs"
down_revision: str | None = "0002_l2c_dataset_split"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX = "^[0-9a-f]{64}$"
_PARTITIONS = ("train", "validation", "test")
_SOURCES = ("v1-inherited", "v2-policy")
_REJECT_MUTATION = "audit.minos_reject_mutation"  # reused from 0001 (append-only)

# Leakage-safe projection (no round_id / sort_order / allocation_digest / truth columns):
# join + integrity identity only. Feature values never appear here — L2-E owns the
# ELIGIBLE-only feature boundary.
_VIEW_COLUMNS = (
    "dr.dataset_id",
    "dr.chromosome",
    "dr.region_source",
    "dr.region_start0",
    "dr.region_end0_exclusive",
    "dr.region_length_bp",
    "dr.region_hash",
    "dr.bam_sha256",
    "dr.bai_sha256",
    "dr.reference_sha256",
    "dr.fai_sha256",
    "dr.parameter_space_hash",
    "dr.feature_registry_hash",
    "ss.epoch",
    "ss.manifest_hash",
    "ss.registry_snapshot_hash",
    "ea.partition",
    "ea.origin_epoch",
    "ea.assignment_source",
)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=False),
        nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def _ts(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _sha(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.CHAR(64), nullable=nullable)


def _hex_ck(col: str, name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} ~ '{_HEX}'"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=name)


def _create_split_snapshots() -> None:
    op.create_table(
        "split_snapshots",
        _uuid_pk(),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("salt", sa.Text(), nullable=False),
        sa.Column("split_policy_version", sa.Text(), nullable=False),
        _sha("policy_hash"),
        _sha("manifest_hash"),
        _sha("registry_snapshot_hash"),
        _sha("ancestor_v1_dataset_registry_hash"),
        _sha("parent_registry_snapshot_hash", nullable=True),
        _sha("parent_manifest_hash", nullable=True),
        sa.Column("parent_snapshot_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("parent_epoch", sa.Integer(), nullable=True),
        sa.Column("transition_count", sa.BigInteger(), nullable=False),
        sa.Column("sample_count", sa.BigInteger(), nullable=False),
        sa.Column("count_train", sa.BigInteger(), nullable=False),
        sa.Column("count_validation", sa.BigInteger(), nullable=False),
        sa.Column("count_test", sa.BigInteger(), nullable=False),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_split_snapshots"),
        sa.ForeignKeyConstraint(
            ["parent_snapshot_id"],
            ["catalog.split_snapshots.id"],
            name="fk_split_snapshots_parent_snapshot_id_split_snapshots",
        ),
        sa.UniqueConstraint("epoch", name="uq_split_snapshots_epoch"),
        sa.UniqueConstraint("manifest_hash", name="uq_split_snapshots_manifest_hash"),
        sa.UniqueConstraint("registry_snapshot_hash", name="uq_split_snapshots_registry_hash"),
        sa.CheckConstraint("epoch >= 1", name="ck_split_snapshots_epoch_positive"),
        # epoch 1 has no parent (id/manifest/registry/epoch all null); later epochs bind
        # a real parent snapshot AND parent hashes AND parent_epoch = epoch - 1.
        sa.CheckConstraint(
            "(epoch = 1 AND parent_epoch IS NULL AND parent_snapshot_id IS NULL "
            " AND parent_manifest_hash IS NULL AND parent_registry_snapshot_hash IS NULL) "
            "OR (epoch > 1 AND parent_epoch = epoch - 1 AND parent_snapshot_id IS NOT NULL "
            " AND parent_manifest_hash IS NOT NULL AND parent_registry_snapshot_hash IS NOT NULL)",
            name="ck_split_snapshots_parent_chain",
        ),
        sa.CheckConstraint("transition_count = 0", name="ck_split_snapshots_no_transitions"),
        sa.CheckConstraint("sample_count >= 0", name="ck_split_snapshots_sample_count_nonneg"),
        sa.CheckConstraint(
            "count_train + count_validation + count_test = sample_count",
            name="ck_split_snapshots_counts_sum",
        ),
        sa.CheckConstraint("length(salt) > 0", name="ck_split_snapshots_salt_nonempty"),
        _hex_ck("policy_hash", "ck_split_snapshots_policy_hash_hex"),
        _hex_ck("manifest_hash", "ck_split_snapshots_manifest_hash_hex"),
        _hex_ck("registry_snapshot_hash", "ck_split_snapshots_registry_hash_hex"),
        _hex_ck("ancestor_v1_dataset_registry_hash", "ck_split_snapshots_ancestor_v1_hex"),
        _hex_ck("parent_manifest_hash", "ck_split_snapshots_parent_manifest_hex", nullable=True),
        _hex_ck(
            "parent_registry_snapshot_hash",
            "ck_split_snapshots_parent_registry_hex",
            nullable=True,
        ),
        schema="catalog",
    )


def _create_epoch_allocations() -> None:
    op.create_table(
        "split_epoch_allocations",
        _uuid_pk(),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("dataset_registry_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("partition", sa.Text(), nullable=False),
        sa.Column("origin_epoch", sa.Integer(), nullable=False),
        sa.Column("assignment_source", sa.Text(), nullable=False),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_split_epoch_allocations"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["catalog.split_snapshots.id"],
            name="fk_split_epoch_allocations_snapshot_id_split_snapshots",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_split_epoch_allocations_dataset_registry_id_dataset_registry",
        ),
        # exactly one allocation per identity per epoch -> overlap impossible within an epoch.
        sa.UniqueConstraint(
            "snapshot_id", "dataset_registry_id", name="uq_split_epoch_allocations_snapshot_dataset"
        ),
        sa.CheckConstraint(
            "partition IN (" + ", ".join(f"'{p}'" for p in _PARTITIONS) + ")",
            name="ck_split_epoch_allocations_partition_valid",
        ),
        sa.CheckConstraint(
            "assignment_source IN (" + ", ".join(f"'{s}'" for s in _SOURCES) + ")",
            name="ck_split_epoch_allocations_source_valid",
        ),
        sa.CheckConstraint("origin_epoch >= 1", name="ck_split_epoch_allocations_origin_positive"),
        schema="catalog",
    )
    op.create_index(
        "ix_split_epoch_allocations_partition",
        "split_epoch_allocations",
        ["partition"],
        schema="catalog",
    )
    op.create_index(
        "ix_split_epoch_allocations_snapshot_id",
        "split_epoch_allocations",
        ["snapshot_id"],
        schema="catalog",
    )


def _create_views() -> None:
    cols = ", ".join(_VIEW_COLUMNS)
    join = (
        "FROM catalog.split_epoch_allocations ea "
        "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id "
        "JOIN catalog.split_snapshots ss ON ss.id = ea.snapshot_id "
    )
    op.execute(
        f"CREATE VIEW catalog.training_epoch_allocations AS "
        f"SELECT {cols} {join} WHERE ea.partition = 'train';"
    )
    # Validation is readable by the evaluator (model selection during development).
    op.execute(
        f"CREATE VIEW evaluation.validation_epoch_allocations AS "
        f"SELECT {cols} {join} WHERE ea.partition = 'validation';"
    )
    # Sealed test cohort: exists but is granted to NO role. It stays unreadable until an
    # explicit, separately-authorized final-evaluation migration grants access.
    op.execute(
        f"CREATE VIEW evaluation.sealed_test_epoch_allocations AS "
        f"SELECT {cols} {join} WHERE ea.partition = 'test';"
    )


def _create_triggers() -> None:
    for table in ("split_snapshots", "split_epoch_allocations"):
        op.execute(
            f"CREATE TRIGGER trg_catalog_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON catalog.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
        )


def _grant() -> None:
    for obj in (
        "catalog.split_snapshots",
        "catalog.split_epoch_allocations",
        "catalog.training_epoch_allocations",
        "evaluation.validation_epoch_allocations",
        "evaluation.sealed_test_epoch_allocations",
    ):
        op.execute(f"REVOKE ALL ON {obj} FROM PUBLIC;")
    # Trainer: only training allocations, only through the owner-defined view.
    op.execute("GRANT SELECT ON catalog.training_epoch_allocations TO minos_trainer;")
    # Evaluator: validation only. The sealed test view is intentionally NOT granted here.
    op.execute("GRANT SELECT ON evaluation.validation_epoch_allocations TO minos_evaluator;")
    # evaluation.sealed_test_epoch_allocations: NO GRANT — access denied until authorized.


def upgrade() -> None:
    op.execute("SET ROLE minos_admin")
    _create_split_snapshots()
    _create_epoch_allocations()
    _create_views()
    _create_triggers()
    _grant()
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    for table in ("split_snapshots", "split_epoch_allocations"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_catalog_{table}_append_only ON catalog.{table};")
    op.execute("DROP VIEW IF EXISTS catalog.training_epoch_allocations;")
    op.execute("DROP VIEW IF EXISTS evaluation.validation_epoch_allocations;")
    op.execute("DROP VIEW IF EXISTS evaluation.sealed_test_epoch_allocations;")
    op.drop_index(
        "ix_split_epoch_allocations_snapshot_id",
        table_name="split_epoch_allocations",
        schema="catalog",
    )
    op.drop_index(
        "ix_split_epoch_allocations_partition",
        table_name="split_epoch_allocations",
        schema="catalog",
    )
    op.drop_table("split_epoch_allocations", schema="catalog")
    op.drop_table("split_snapshots", schema="catalog")
    # v1 split_allocations, dataset_registry, and all L2-B objects are left intact.
    op.execute("RESET ROLE")
