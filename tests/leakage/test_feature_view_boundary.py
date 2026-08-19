"""L2-E leakage boundary: what the production feature view can NEVER contain.

Proves the authoritative extraction surface excludes window-profile fields, truth or
mutation labels, scores, and split-allocation percentages; that matrices are strictly
partition-isolated with no test surface; and that no raw profile document is exposed
through the production matrix API.
"""

from __future__ import annotations

import pytest

from minos_engine.layer2.feature_registry import production_eligible_fields, record_for
from minos_engine.layer2.features.contracts import (
    AUTHORITATIVE_COLUMNS,
    FeatureMatrix,
    FeatureVector,
    Partition,
)
from minos_engine.layer2.features.extraction import (
    ExtractionResult,
    MatrixBuild,
    SnapshotMember,
    VerificationResult,
)

_FORBIDDEN_TERMS = ("truth", "mutation", "label", "score", "split", "partition", "percent")


def test_authoritative_columns_carry_no_forbidden_terms() -> None:
    for path in AUTHORITATIVE_COLUMNS:
        lowered = path.lower()
        for term in _FORBIDDEN_TERMS:
            assert term not in lowered, f"forbidden term {term!r} in feature path {path}"


def test_window_profile_fields_are_fully_excluded() -> None:
    window_eligible = [
        p
        for p in production_eligible_fields()
        if (r := record_for(p)) is not None and r.source_schema == "window-profile-v1"
    ]
    assert len(window_eligible) == 12
    assert set(window_eligible).isdisjoint(AUTHORITATIVE_COLUMNS)
    assert all(
        (r := record_for(p)) is not None and r.source_schema == "bam-profile-v1"
        for p in AUTHORITATIVE_COLUMNS
    )


def test_no_split_allocation_behavior_membership_is_verbatim() -> None:
    """Behavioral proof: partition sizes that CANNOT arise from any fixed percentage
    scheme are consumed verbatim — assembly never reallocates or scales membership."""
    from minos_engine.layer2.features.extraction import FrozenSnapshot

    members = tuple(
        SnapshotMember(
            dataset_id=f"ds-{i:02d}",
            profile_id=f"p-{i:02d}",
            partition=partition,  # type: ignore[arg-type]
            content_hash="a" * 64,
            feature_values_hash="b" * 64,
            profile_sha256="c" * 64,
        )
        # 1 train / 4 validation / 2 test: no percentage rule produces this shape.
        for i, partition in enumerate(
            ("train", "validation", "validation", "validation", "validation", "test", "test")
        )
    )
    snapshot = FrozenSnapshot(epoch=9, snapshot_hash="d" * 64, members=members)
    assert [m.dataset_id for m in snapshot.members_for("train")] == ["ds-00"]
    assert len(snapshot.members_for("validation")) == 4
    # counts derive ONLY from the verbatim assignments — nothing was moved or scaled.
    assert {m.partition for m in snapshot.members} == {"train", "validation", "test"}


def test_partition_type_is_structurally_train_validation_only() -> None:
    from typing import get_args

    assert set(get_args(Partition)) == {"train", "validation"}


def test_no_raw_profile_document_exposed_by_matrix_api() -> None:
    # every model on the production path exposes ONLY hashes, identities and values —
    # never a parsed or raw profile document.
    assert set(ExtractionResult.model_fields) == {"values", "feature_values_hash"}
    assert set(MatrixBuild.model_fields) == {"matrix", "vectors"}
    assert set(VerificationResult.model_fields) == {"checks"}
    assert "document" not in FeatureVector.model_fields
    assert "profile" not in FeatureVector.model_fields
    for field in FeatureVector.model_fields:
        assert "document" not in field and "payload" not in field
    for field in FeatureMatrix.model_fields:
        assert "document" not in field and "payload" not in field
    assert "profile_sha256" not in FeatureVector.model_fields  # artifact bytes stay behind
    # the snapshot member is metadata-only: hashes, never payload bytes.
    for field, info in SnapshotMember.model_fields.items():
        assert info.annotation is not bytes, f"{field} must not carry payload bytes"


def test_test_partition_has_no_contract_surface() -> None:
    from pydantic import ValidationError

    from minos_engine.layer2.features.errors import ForbiddenPartitionError
    from minos_engine.layer2.features.extraction import FrozenSnapshot

    members = (
        SnapshotMember(
            dataset_id="ds-1",
            profile_id="p-1",
            partition="train",
            content_hash="a" * 64,
            feature_values_hash="b" * 64,
            profile_sha256="c" * 64,
        ),
        SnapshotMember(
            dataset_id="ds-2",
            profile_id="p-2",
            partition="test",
            content_hash="a" * 64,
            feature_values_hash="b" * 64,
            profile_sha256="c" * 64,
        ),
    )
    snapshot = FrozenSnapshot(epoch=1, snapshot_hash="d" * 64, members=members)
    with pytest.raises(ForbiddenPartitionError):
        snapshot.members_for("test")
    with pytest.raises(ValidationError):
        FeatureVector(
            epoch=1,
            dataset_id="ds-2",
            profile_id="p-2",
            content_hash="a" * 64,
            feature_values_hash="b" * 64,
            partition="test",  # type: ignore[arg-type]
            snapshot_hash="d" * 64,
            registry_hash="0" * 64,
            feature_set_hash="0" * 64,
            value_count=129,
            values=tuple(0.5 for _ in range(129)),
        )
