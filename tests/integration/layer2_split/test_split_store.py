"""L2-C split store: counts, immutability (append-only), and constraint enforcement."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError

from minos_engine.storage.dataset_split import partition_counts


def test_partition_counts_50_10_15(l2c_engine: Engine):
    with l2c_engine.connect() as c:
        counts = partition_counts(c)
    assert counts == {"train": 50, "validation": 10, "test": 15}


def test_registry_and_allocation_append_only(l2c_engine: Engine):
    # UPDATE and DELETE are rejected on both L2-C evidence tables (immutable identity
    # + immutable partition assignment), enforced by the append-only trigger.
    statements = (
        "UPDATE catalog.dataset_registry SET round_id = 'tampered'",
        "DELETE FROM catalog.dataset_registry",
        "UPDATE catalog.split_allocations SET partition = 'test'",
        "DELETE FROM catalog.split_allocations",
    )
    for sql in statements:
        with l2c_engine.connect() as c, pytest.raises(DatabaseError):
            c.execute(text(sql))
            c.commit()


def test_duplicate_and_overlap_rejected(l2c_engine: Engine):
    # A second allocation row for an already-allocated dataset violates the
    # one-partition-per-dataset unique constraint (split overlap is impossible).
    with l2c_engine.connect() as c:
        reg_id = c.execute(
            text("SELECT dataset_registry_id FROM catalog.split_allocations LIMIT 1")
        ).scalar()
        with pytest.raises(DatabaseError):
            c.execute(
                text(
                    "INSERT INTO catalog.split_allocations "
                    "(dataset_registry_id, partition, sort_order, manifest_hash) "
                    "VALUES (:r, 'validation', 99, :h)"
                ),
                {"r": reg_id, "h": "a" * 64},
            )
            c.commit()

    # A duplicate dataset_id in the registry violates the unique constraint.
    with l2c_engine.connect() as c:
        existing = c.execute(
            text("SELECT dataset_id FROM catalog.dataset_registry LIMIT 1")
        ).scalar()
        with pytest.raises(DatabaseError):
            c.execute(
                text(
                    "INSERT INTO catalog.dataset_registry "
                    "(dataset_id, round_id, chromosome, region_source, region_start0, "
                    " region_end0_exclusive, region_length_bp, region_coordinate_system, "
                    " region_hash, bam_sha256, bai_sha256, reference_sha256, fai_sha256, "
                    " bam_size_bytes, parameter_space_hash, feature_registry_hash, "
                    " identity_tuple_hash, manifest_hash, split_algorithm_version, split_salt, "
                    " allocation_digest) VALUES "
                    "(:d, 'r', 'chr18', 'chr18:0-10', 0, 10, 10, 'zero_based_half_open', "
                    " :h,:h,:h,:h,:h, 1, :h,:h,:h,:h, 'v','s',:h)"
                ),
                {"d": existing, "h": "b" * 64},
            )
            c.commit()


def test_invalid_partition_value_rejected(l2c_engine: Engine):
    with l2c_engine.connect() as c:
        reg_id = c.execute(
            text("SELECT dataset_registry_id FROM catalog.split_allocations LIMIT 1")
        ).scalar()
        with pytest.raises(DatabaseError):
            c.execute(
                text(
                    "INSERT INTO catalog.split_allocations "
                    "(dataset_registry_id, partition, sort_order, manifest_hash) "
                    "VALUES (:r, 'holdout', 0, :h)"
                ),
                {"r": reg_id, "h": "c" * 64},
            )
            c.commit()
