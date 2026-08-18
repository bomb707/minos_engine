"""L2-C dataset-split persistence — Core tables on a dedicated metadata.

These :class:`sqlalchemy.Table` definitions mirror the frozen migration
``0002_l2c_dataset_split`` and exist ONLY to build inserts/selects for the
split-allocation store. They live on a private ``MetaData`` that is intentionally
**not** part of the L2-B ``Base.metadata`` (``storage/metadata.py``), so the accepted
L2-B ``storage_schema_hash`` — and the DB-READY gate that binds it — is never changed.
The authoritative DDL is the migration; nothing here creates or drops schema.

Persistence runs as the schema owner (``minos_admin``) — application roles have no
direct grant on the base tables. Rows are append-only (identity + partition are
immutable after insert, enforced by the migration triggers).
"""

from __future__ import annotations

from sqlalchemy import (
    CHAR,
    BigInteger,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from minos_engine.layer2.split.contracts import DatasetSplitManifest

__all__ = [
    "l2c_metadata",
    "dataset_registry",
    "split_allocations",
    "dataset_evaluation_identity",
    "persist_manifest",
    "partition_counts",
    "register_evaluation_identity",
    "dataset_registry_id_for",
]

l2c_metadata = MetaData()


def _uuid() -> Column[str]:
    return Column(
        "id", UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )


dataset_registry = Table(
    "dataset_registry",
    l2c_metadata,
    _uuid(),
    Column("dataset_id", String, nullable=False, unique=True),
    Column("round_id", String, nullable=False),
    Column("chromosome", String, nullable=False),
    Column("region_source", String, nullable=False),
    Column("region_start0", BigInteger, nullable=False),
    Column("region_end0_exclusive", BigInteger, nullable=False),
    Column("region_length_bp", BigInteger, nullable=False),
    Column("region_coordinate_system", String, nullable=False),
    Column("region_hash", CHAR(64), nullable=False),
    Column("bam_sha256", CHAR(64), nullable=False),
    Column("bai_sha256", CHAR(64), nullable=False),
    Column("reference_sha256", CHAR(64), nullable=False),
    Column("fai_sha256", CHAR(64), nullable=False),
    Column("bam_size_bytes", BigInteger, nullable=False),
    Column("parameter_space_hash", CHAR(64), nullable=False),
    Column("feature_registry_hash", CHAR(64), nullable=False),
    Column("identity_tuple_hash", CHAR(64), nullable=False),
    Column("manifest_hash", CHAR(64), nullable=False),
    Column("split_algorithm_version", String, nullable=False),
    Column("split_salt", String, nullable=False),
    Column("allocation_digest", CHAR(64), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    schema="catalog",
)

split_allocations = Table(
    "split_allocations",
    l2c_metadata,
    _uuid(),
    Column(
        "dataset_registry_id",
        UUID(as_uuid=False),
        ForeignKey("catalog.dataset_registry.id"),
        nullable=False,
        unique=True,
    ),
    Column("partition", String, nullable=False),
    Column("sort_order", Integer, nullable=False),
    Column("manifest_hash", CHAR(64), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    schema="catalog",
)

dataset_evaluation_identity = Table(
    "dataset_evaluation_identity",
    l2c_metadata,
    _uuid(),
    Column(
        "dataset_registry_id",
        UUID(as_uuid=False),
        ForeignKey("catalog.dataset_registry.id"),
        nullable=False,
        unique=True,
    ),
    Column("truth_vcf_sha256", CHAR(64), nullable=True),
    Column("truth_tbi_sha256", CHAR(64), nullable=True),
    Column("mutations_vcf_sha256", CHAR(64), nullable=True),
    Column("mutations_tbi_sha256", CHAR(64), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    schema="evaluation",
)


def persist_manifest(conn: Connection, manifest: DatasetSplitManifest) -> int:
    """Insert the manifest's registry + allocation rows (owner connection). Returns count.

    Runs inside the caller's transaction. Uniqueness and the one-partition-per-dataset
    constraint make duplicate insertion impossible at the database layer.
    """
    written = 0
    for s in manifest.samples:
        reg_id = conn.execute(
            insert(dataset_registry)
            .values(
                dataset_id=s.dataset_id,
                round_id=s.round_id,
                chromosome=s.chromosome,
                region_source=s.region_source,
                region_start0=s.region_start0,
                region_end0_exclusive=s.region_end0_exclusive,
                region_length_bp=s.region_length_bp,
                region_coordinate_system=s.region_coordinate_system,
                region_hash=s.region_hash,
                bam_sha256=s.bam_sha256,
                bai_sha256=s.bai_sha256,
                reference_sha256=s.reference_sha256,
                fai_sha256=s.fai_sha256,
                bam_size_bytes=s.bam_size_bytes,
                parameter_space_hash=s.parameter_space_hash,
                feature_registry_hash=s.feature_registry_hash,
                identity_tuple_hash=s.identity_tuple_hash,
                manifest_hash=manifest.manifest_hash,
                split_algorithm_version=s.split_algorithm_version,
                split_salt=s.split_salt,
                allocation_digest=s.allocation_digest,
            )
            .returning(dataset_registry.c.id)
        ).scalar_one()
        conn.execute(
            insert(split_allocations).values(
                dataset_registry_id=reg_id,
                partition=s.partition,
                sort_order=s.sort_order,
                manifest_hash=manifest.manifest_hash,
            )
        )
        written += 1
    return written


def partition_counts(conn: Connection) -> dict[str, int]:
    """Return {partition: row_count} from the split_allocations base table."""
    rows = conn.execute(
        select(split_allocations.c.partition, func.count()).group_by(split_allocations.c.partition)
    ).all()
    return {p: int(n) for p, n in rows}


def register_evaluation_identity(
    conn: Connection,
    dataset_registry_id: str,
    *,
    truth_vcf_sha256: str | None = None,
    truth_tbi_sha256: str | None = None,
    mutations_vcf_sha256: str | None = None,
    mutations_tbi_sha256: str | None = None,
) -> None:
    """Record truth/mutation cryptographic identities (evaluator/admin only path)."""
    conn.execute(
        insert(dataset_evaluation_identity).values(
            dataset_registry_id=dataset_registry_id,
            truth_vcf_sha256=truth_vcf_sha256,
            truth_tbi_sha256=truth_tbi_sha256,
            mutations_vcf_sha256=mutations_vcf_sha256,
            mutations_tbi_sha256=mutations_tbi_sha256,
        )
    )


def dataset_registry_id_for(conn: Connection, dataset_id: str) -> str | None:
    """Look up the surrogate id for a dataset_id (owner/admin helper)."""
    return conn.execute(
        text("SELECT id FROM catalog.dataset_registry WHERE dataset_id = :d"),
        {"d": dataset_id},
    ).scalar()
