"""L2-C v2 epoch persistence on real PG16: inherited 50/10/15, append-only, fail-closed."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError

from minos_engine.common.errors import ContractValidationError


def test_snapshot_row_records_epoch1_counts(l2c_v2_engine: Engine) -> None:
    with l2c_v2_engine.connect() as c:
        row = c.execute(
            text(
                "SELECT epoch, parent_epoch, transition_count, sample_count, count_train, "
                "count_validation, count_test, parent_snapshot_id "
                "FROM catalog.split_snapshots"
            )
        ).one()
    assert tuple(row) == (1, None, 0, 75, 50, 10, 15, None)


def test_snapshot_binds_lineage_hashes(l2c_v2_engine: Engine) -> None:
    with l2c_v2_engine.connect() as c:
        row = c.execute(
            text(
                "SELECT registry_snapshot_hash, parent_registry_snapshot_hash, "
                "parent_manifest_hash FROM catalog.split_snapshots"
            )
        ).one()
    registry_hash, parent_reg, parent_man = row
    assert len(registry_hash) == 64
    assert parent_reg is None and parent_man is None  # epoch 1 has no parent


def test_epoch_allocations_are_inherited_50_10_15(l2c_v2_engine: Engine) -> None:
    with l2c_v2_engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT partition, count(*) FROM catalog.split_epoch_allocations GROUP BY partition"
            )
        ).all()
        sources = c.execute(
            text(
                "SELECT DISTINCT assignment_source, origin_epoch "
                "FROM catalog.split_epoch_allocations"
            )
        ).all()
    counts = {p: int(n) for p, n in rows}
    assert counts == {"train": 50, "validation": 10, "test": 15}
    assert sum(counts.values()) == 75
    # epoch 1 is a pure v1 inheritance: every row marked v1-inherited @ origin 1.
    assert [tuple(r) for r in sources] == [("v1-inherited", 1)]


def test_per_chromosome_is_10_2_3(l2c_v2_engine: Engine) -> None:
    with l2c_v2_engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT dr.chromosome, ea.partition, count(*) "
                "FROM catalog.split_epoch_allocations ea "
                "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id "
                "GROUP BY dr.chromosome, ea.partition"
            )
        ).all()
    per: dict[str, dict[str, int]] = {}
    for chrom, part, n in rows:
        per.setdefault(chrom, {})[part] = int(n)
    assert set(per) == {"chr18", "chr19", "chr20", "chr21", "chr22"}
    for chrom, counts in per.items():
        assert counts == {"train": 10, "validation": 2, "test": 3}, chrom


def test_one_allocation_per_identity(l2c_v2_engine: Engine) -> None:
    with l2c_v2_engine.connect() as c:
        distinct = c.execute(
            text("SELECT count(DISTINCT dataset_registry_id) FROM catalog.split_epoch_allocations")
        ).scalar()
        total = c.execute(text("SELECT count(*) FROM catalog.split_epoch_allocations")).scalar()
    assert distinct == total == 75


# --------------------------------------------------------------------------- #
# partition-separated, minimized views
# --------------------------------------------------------------------------- #
def test_training_view_exposes_only_train_partition(l2c_v2_engine: Engine) -> None:
    with l2c_v2_engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM catalog.training_epoch_allocations")).scalar()
        parts = (
            c.execute(text("SELECT DISTINCT partition FROM catalog.training_epoch_allocations"))
            .scalars()
            .all()
        )
    assert n == 50
    assert set(parts) == {"train"}


def test_validation_view_exposes_only_validation(l2c_v2_engine: Engine) -> None:
    with l2c_v2_engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM evaluation.validation_epoch_allocations")).scalar()
        parts = (
            c.execute(
                text("SELECT DISTINCT partition FROM evaluation.validation_epoch_allocations")
            )
            .scalars()
            .all()
        )
    assert n == 10
    assert set(parts) == {"validation"}


def test_sealed_test_view_exposes_only_test(l2c_v2_engine: Engine) -> None:
    # As the owner connection (test fixture), the sealed view exists and filters to test;
    # role-level denial is proven in test_role_isolation.
    with l2c_v2_engine.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM evaluation.sealed_test_epoch_allocations")
        ).scalar()
        parts = (
            c.execute(
                text("SELECT DISTINCT partition FROM evaluation.sealed_test_epoch_allocations")
            )
            .scalars()
            .all()
        )
    assert n == 15
    assert set(parts) == {"test"}


def test_views_hide_round_id_and_digest(l2c_v2_engine: Engine) -> None:
    """The approved projection must not surface round_id / allocation_digest / truth."""
    with l2c_v2_engine.connect() as c:
        cols = (
            c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'catalog' AND table_name = 'training_epoch_allocations'"
                )
            )
            .scalars()
            .all()
        )
    forbidden = {"round_id", "sort_order", "allocation_digest", "truth_vcf_sha256"}
    assert forbidden.isdisjoint(cols)
    assert {"epoch", "manifest_hash", "registry_snapshot_hash", "partition"} <= set(cols)


def test_views_have_no_feature_columns(l2c_v2_engine: Engine) -> None:
    """Trainer/validation views carry join/integrity identity ONLY — never feature values.

    Coordinates, chromosome, artifact URIs/hashes are integrity/join fields, not model
    features; the ELIGIBLE-only feature boundary is owned by L2-E.
    """
    allowed = {
        "dataset_id",
        "chromosome",
        "region_source",
        "region_start0",
        "region_end0_exclusive",
        "region_length_bp",
        "region_hash",
        "bam_sha256",
        "bai_sha256",
        "reference_sha256",
        "fai_sha256",
        "parameter_space_hash",
        "feature_registry_hash",
        "epoch",
        "manifest_hash",
        "registry_snapshot_hash",
        "partition",
        "origin_epoch",
        "assignment_source",
    }
    with l2c_v2_engine.connect() as c:
        for schema, view in (
            ("catalog", "training_epoch_allocations"),
            ("evaluation", "validation_epoch_allocations"),
            ("evaluation", "sealed_test_epoch_allocations"),
        ):
            cols = set(
                c.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :s AND table_name = :t"
                    ),
                    {"s": schema, "t": view},
                ).scalars()
            )
            assert cols <= allowed, (view, cols - allowed)
            # no profile/feature/score column can appear
            assert not any(
                tok in col for col in cols for tok in ("feature_value", "profile", "score")
            )


# --------------------------------------------------------------------------- #
# append-only + fail-closed
# --------------------------------------------------------------------------- #
def test_epoch_tables_append_only(l2c_v2_engine: Engine) -> None:
    # UPDATE and DELETE are rejected on both v2 evidence tables by the append-only trigger,
    # even for the schema owner (fresh connection per statement so an abort never poisons
    # the next assertion).
    statements = (
        "UPDATE catalog.split_snapshots SET epoch = 2",
        "DELETE FROM catalog.split_snapshots",
        "UPDATE catalog.split_epoch_allocations SET partition = 'train'",
        "DELETE FROM catalog.split_epoch_allocations",
    )
    for sql in statements:
        with l2c_v2_engine.connect() as c, pytest.raises(DatabaseError):
            c.execute(text("SET ROLE minos_admin"))
            c.execute(text(sql))
            c.commit()


def test_repersisting_same_epoch_rejected(l2c_v2_engine: Engine) -> None:
    """UNIQUE(epoch)/UNIQUE(manifest_hash)/UNIQUE(registry_snapshot_hash) forbid re-write.

    ``pytest.raises`` wraps the transaction (not the reverse) so the exception propagates
    through ``engine.begin()`` and the partial write is ROLLED BACK, never committed.
    """
    from minos_engine.storage.dataset_split_v2 import persist_epoch
    from tests.integration.layer2_split_v2.conftest import (
        synthetic_epoch1_manifest,
        synthetic_v1_manifest,
    )

    with pytest.raises(DatabaseError), l2c_v2_engine.begin() as c:
        persist_epoch(c, synthetic_epoch1_manifest(), v1_manifest=synthetic_v1_manifest())


def test_unverified_manifest_fails_closed_before_insert(l2c_v2_engine: Engine) -> None:
    """A manifest that fails the complete verifier is rejected BEFORE any row is written."""
    from minos_engine.storage.dataset_split_v2 import persist_epoch
    from tests.integration.layer2_split_v2.conftest import synthetic_epoch1_manifest

    bad = synthetic_epoch1_manifest()
    bad = {**bad, "transition_count": 1}  # invalid: must be 0 (also breaks manifest_hash)
    with pytest.raises(ContractValidationError), l2c_v2_engine.begin() as c:
        persist_epoch(c, bad)


def test_unregistered_identity_fails_closed(l2c_v2_engine: Engine) -> None:
    """An epoch-2 sample whose full identity is not in dataset_registry is rejected,
    and the aborted transaction leaves NO partial epoch rows behind."""
    from minos_engine.layer2.split_v2.generator import build_next_epoch_manifest
    from minos_engine.storage.dataset_split_v2 import persist_epoch
    from tests.integration.layer2_split_v2.conftest import synthetic_epoch1_manifest

    ghost = [
        {
            "dataset_id": "minos-chr18-ghost",
            "round_id": "9" * 16,
            "chromosome": "chr18",
            "identity_tuple_hash": "f" * 64,
        }
    ]
    m2 = build_next_epoch_manifest(synthetic_epoch1_manifest(), ghost)
    with pytest.raises(ContractValidationError), l2c_v2_engine.begin() as c:
        persist_epoch(c, m2)
    # the failed epoch-2 attempt must leave no snapshot and no allocations behind.
    with l2c_v2_engine.connect() as c:
        snapshots = c.execute(text("SELECT count(*) FROM catalog.split_snapshots")).scalar()
        allocations = c.execute(
            text("SELECT count(*) FROM catalog.split_epoch_allocations")
        ).scalar()
    assert snapshots == 1  # only epoch 1
    assert allocations == 75
