"""L2-C dataset registry + deterministic split allocation — immutable snapshot.

Revision ID: 0002_l2c_dataset_split
Revises: 0001_l2b_initial
Create Date: 2026-08-18

A FROZEN snapshot, self-contained like ``0001_l2b_initial``: it imports no ORM
declarative base or model metadata and never bulk-emits or reflects an ORM schema —
every table, column, constraint, index, view, trigger, and grant is written
explicitly, so replaying revision 0002 always produces the exact L2-C inventory
regardless of later ORM changes.

Ownership + isolation: all new objects are created while executing ``SET ROLE
minos_admin`` (so ``minos_admin`` owns them). Identity and partition assignment are
immutable after insert (append-only trigger reusing ``audit.minos_reject_mutation``).
Partition isolation is enforced at the SQL authorization layer: application roles get
NO direct grant on the base allocation tables; ``minos_trainer`` may read only
training allocations (through an owner-defined view), and the locked validation/test
allocations are reachable only through an evaluator-restricted view. ``minos_live``
and ``minos_runner`` receive no split-allocation access. Truth/mutation cryptographic
identities live only in the evaluation schema (evaluator/admin only).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_l2c_dataset_split"
down_revision: str | None = "0001_l2b_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX = "^[0-9a-f]{64}$"
_CHROMS = ("chr18", "chr19", "chr20", "chr21", "chr22")
_PARTITIONS = ("train", "validation", "test")
_REJECT_MUTATION = "audit.minos_reject_mutation"  # reused from 0001 (append-only)

# minimal, approved projection exposed by allocation views (no round_id / sort_order /
# allocation_digest — those are position/label proxies excluded by the leakage policy).
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
    "sa_alloc.partition",
    "sa_alloc.manifest_hash",
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
    expr = f"{col} IS NULL OR {col} ~ '{_HEX}'" if nullable else f"{col} ~ '{_HEX}'"
    return sa.CheckConstraint(expr, name=name)


def _create_dataset_registry() -> None:
    op.create_table(
        "dataset_registry",
        _uuid_pk(),
        sa.Column("dataset_id", sa.Text(), nullable=False),
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("chromosome", sa.Text(), nullable=False),
        sa.Column("region_source", sa.Text(), nullable=False),
        sa.Column("region_start0", sa.BigInteger(), nullable=False),
        sa.Column("region_end0_exclusive", sa.BigInteger(), nullable=False),
        sa.Column("region_length_bp", sa.BigInteger(), nullable=False),
        sa.Column("region_coordinate_system", sa.Text(), nullable=False),
        _sha("region_hash"),
        _sha("bam_sha256"),
        _sha("bai_sha256"),
        _sha("reference_sha256"),
        _sha("fai_sha256"),
        sa.Column("bam_size_bytes", sa.BigInteger(), nullable=False),
        _sha("parameter_space_hash"),
        _sha("feature_registry_hash"),
        _sha("identity_tuple_hash"),
        _sha("manifest_hash"),
        sa.Column("split_algorithm_version", sa.Text(), nullable=False),
        sa.Column("split_salt", sa.Text(), nullable=False),
        _sha("allocation_digest"),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_registry"),
        sa.UniqueConstraint("dataset_id", name="uq_dataset_registry_dataset_id"),
        sa.UniqueConstraint("identity_tuple_hash", name="uq_dataset_registry_identity_tuple_hash"),
        sa.UniqueConstraint(
            "chromosome", "round_id", name="uq_dataset_registry_chromosome_round_id"
        ),
        sa.UniqueConstraint(
            "bam_sha256",
            "bai_sha256",
            "reference_sha256",
            "fai_sha256",
            "region_hash",
            name="uq_dataset_registry_identity_tuple",
        ),
        sa.CheckConstraint(
            "chromosome IN (" + ", ".join(f"'{c}'" for c in _CHROMS) + ")",
            name="ck_dataset_registry_chromosome_valid",
        ),
        sa.CheckConstraint(
            "length(dataset_id) > 0", name="ck_dataset_registry_dataset_id_nonempty"
        ),
        sa.CheckConstraint("length(round_id) > 0", name="ck_dataset_registry_round_id_nonempty"),
        sa.CheckConstraint("region_start0 >= 0", name="ck_dataset_registry_region_start0_nonneg"),
        sa.CheckConstraint(
            "region_end0_exclusive > region_start0", name="ck_dataset_registry_region_bounds"
        ),
        sa.CheckConstraint(
            "region_length_bp = region_end0_exclusive - region_start0",
            name="ck_dataset_registry_region_length",
        ),
        sa.CheckConstraint("bam_size_bytes >= 0", name="ck_dataset_registry_bam_size_nonneg"),
        _hex_ck("region_hash", "ck_dataset_registry_region_hash_hex"),
        _hex_ck("bam_sha256", "ck_dataset_registry_bam_sha256_hex"),
        _hex_ck("bai_sha256", "ck_dataset_registry_bai_sha256_hex"),
        _hex_ck("reference_sha256", "ck_dataset_registry_reference_sha256_hex"),
        _hex_ck("fai_sha256", "ck_dataset_registry_fai_sha256_hex"),
        _hex_ck("parameter_space_hash", "ck_dataset_registry_parameter_space_hash_hex"),
        _hex_ck("feature_registry_hash", "ck_dataset_registry_feature_registry_hash_hex"),
        _hex_ck("identity_tuple_hash", "ck_dataset_registry_identity_tuple_hash_hex"),
        _hex_ck("manifest_hash", "ck_dataset_registry_manifest_hash_hex"),
        _hex_ck("allocation_digest", "ck_dataset_registry_allocation_digest_hex"),
        schema="catalog",
    )
    op.create_index(
        "ix_dataset_registry_chromosome", "dataset_registry", ["chromosome"], schema="catalog"
    )


def _create_split_allocations() -> None:
    op.create_table(
        "split_allocations",
        _uuid_pk(),
        sa.Column("dataset_registry_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("partition", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        _sha("manifest_hash"),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_split_allocations"),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_split_allocations_dataset_registry_id_dataset_registry",
        ),
        # exactly one partition per dataset -> split overlap is impossible.
        sa.UniqueConstraint("dataset_registry_id", name="uq_split_allocations_dataset_registry_id"),
        sa.CheckConstraint(
            "partition IN (" + ", ".join(f"'{p}'" for p in _PARTITIONS) + ")",
            name="ck_split_allocations_partition_valid",
        ),
        sa.CheckConstraint("sort_order >= 0", name="ck_split_allocations_sort_order_nonneg"),
        _hex_ck("manifest_hash", "ck_split_allocations_manifest_hash_hex"),
        schema="catalog",
    )
    op.create_index(
        "ix_split_allocations_partition", "split_allocations", ["partition"], schema="catalog"
    )


def _create_evaluation_identity() -> None:
    op.create_table(
        "dataset_evaluation_identity",
        _uuid_pk(),
        sa.Column("dataset_registry_id", postgresql.UUID(as_uuid=False), nullable=False),
        _sha("truth_vcf_sha256", nullable=True),
        _sha("truth_tbi_sha256", nullable=True),
        _sha("mutations_vcf_sha256", nullable=True),
        _sha("mutations_tbi_sha256", nullable=True),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_evaluation_identity"),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_dataset_eval_identity_registry_id",
        ),
        sa.UniqueConstraint("dataset_registry_id", name="uq_dataset_eval_identity_registry_id"),
        _hex_ck("truth_vcf_sha256", "ck_dataset_evaluation_identity_truth_vcf_hex", nullable=True),
        _hex_ck("truth_tbi_sha256", "ck_dataset_evaluation_identity_truth_tbi_hex", nullable=True),
        _hex_ck(
            "mutations_vcf_sha256",
            "ck_dataset_evaluation_identity_mutations_vcf_hex",
            nullable=True,
        ),
        _hex_ck(
            "mutations_tbi_sha256",
            "ck_dataset_evaluation_identity_mutations_tbi_hex",
            nullable=True,
        ),
        schema="evaluation",
    )


def _create_views() -> None:
    cols = ", ".join(_VIEW_COLUMNS)
    op.execute(
        f"CREATE VIEW catalog.training_dataset_allocations AS "
        f"SELECT {cols} FROM catalog.split_allocations sa_alloc "
        f"JOIN catalog.dataset_registry dr ON dr.id = sa_alloc.dataset_registry_id "
        f"WHERE sa_alloc.partition = 'train';"
    )
    # The locked (validation/test) view lives in the evaluation schema so that only
    # minos_evaluator (which alone holds evaluation USAGE) can reach it; the owner-
    # defined view still reads the admin-owned catalog base tables.
    op.execute(
        f"CREATE VIEW evaluation.locked_allocations AS "
        f"SELECT {cols} FROM catalog.split_allocations sa_alloc "
        f"JOIN catalog.dataset_registry dr ON dr.id = sa_alloc.dataset_registry_id "
        f"WHERE sa_alloc.partition IN ('validation', 'test');"
    )


def _create_triggers() -> None:
    for schema, table in (
        ("catalog", "dataset_registry"),
        ("catalog", "split_allocations"),
        ("evaluation", "dataset_evaluation_identity"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{schema}_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {schema}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
        )


def _grant() -> None:
    # Base allocation tables: NO grant to any application role (admin owns them).
    # PUBLIC is already revoked by the L2-B default privileges; be explicit anyway.
    for obj in (
        "catalog.dataset_registry",
        "catalog.split_allocations",
        "catalog.training_dataset_allocations",
        "evaluation.locked_allocations",
        "evaluation.dataset_evaluation_identity",
    ):
        op.execute(f"REVOKE ALL ON {obj} FROM PUBLIC;")
    # Trainer: only training allocations, only through the owner-defined view.
    op.execute("GRANT SELECT ON catalog.training_dataset_allocations TO minos_trainer;")
    # Evaluator/admin: the only authorized locked validation/test allocation path.
    op.execute("GRANT SELECT ON evaluation.locked_allocations TO minos_evaluator;")
    # Evaluator records/reads truth/mutation cryptographic identities (evaluation schema
    # USAGE is evaluator-only from 0001, so this stays inaccessible to other roles).
    op.execute("GRANT SELECT, INSERT ON evaluation.dataset_evaluation_identity TO minos_evaluator;")


def upgrade() -> None:
    op.execute("SET ROLE minos_admin")
    _create_dataset_registry()
    _create_split_allocations()
    _create_evaluation_identity()
    _create_views()
    _create_triggers()
    _grant()
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    for schema, table in (
        ("catalog", "dataset_registry"),
        ("catalog", "split_allocations"),
        ("evaluation", "dataset_evaluation_identity"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{schema}_{table}_append_only ON {schema}.{table};")
    op.execute("DROP VIEW IF EXISTS catalog.training_dataset_allocations;")
    op.execute("DROP VIEW IF EXISTS evaluation.locked_allocations;")
    # revoke L2-C grants from the application roles (idempotent).
    op.execute("REVOKE ALL ON evaluation.dataset_evaluation_identity FROM minos_evaluator;")
    op.drop_table("dataset_evaluation_identity", schema="evaluation")
    op.drop_index(
        "ix_split_allocations_partition", table_name="split_allocations", schema="catalog"
    )
    op.drop_table("split_allocations", schema="catalog")
    op.drop_index("ix_dataset_registry_chromosome", table_name="dataset_registry", schema="catalog")
    op.drop_table("dataset_registry", schema="catalog")
    # audit.minos_reject_mutation and all L2-B objects/roles/schemas are left intact.
    op.execute("RESET ROLE")
