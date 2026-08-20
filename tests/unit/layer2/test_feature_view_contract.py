"""E5 canonical feature-view contract — determinism, schema binding, tamper (unit)."""

from __future__ import annotations

import pytest

from minos_engine.layer2.features.contracts import build_feature_set_manifest
from minos_engine.layer2.features.feature_view import (
    FEATURE_VIEW_VERSION,
    FeatureViewColumn,
    FeatureViewManifest,
    FeatureViewMember,
    build_feature_view_manifest,
)

_H = {
    "snapshot_hash": "cf717ebb44e76a3408e975e027b51139df28d643dd1616c5edbce3643182c4c7",
    "split_manifest_hash": "b23cd5716ab46033f7ea0bf123cc9b2a5f401fa37dbffddba8d4201f5ea76145",
    "registry_snapshot_hash": "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675",
    "matrix_hash": "c6a8db848318e5c78839474fa62a4e8e408157a1e6f5cb1bdd18c9cd3d0118b2",
    "artifact_sha256": "0396cb07734a18df803ac813d9d1224ecdc3ec9901d7b8a202ac8c6538f3c243",
}


def _members(n: int) -> tuple[FeatureViewMember, ...]:
    return tuple(
        FeatureViewMember(
            dataset_id=f"ds-{i:02d}",
            member_index=i,
            vector_hash=f"{i:064x}",
            feature_values_hash=f"{i + 1000:064x}",
        )
        for i in range(n)
    )


def _fv(partition: str = "train", n: int = 3) -> FeatureViewManifest:
    return build_feature_view_manifest(
        epoch=1, partition=partition, row_count=n, members=_members(n), **_H
    )


def test_deterministic_identity_byte_stable() -> None:
    a, b = _fv(), _fv()
    assert a.feature_view_hash == b.feature_view_hash
    assert a.model_dump_json() == b.model_dump_json()
    assert a.feature_view_version == FEATURE_VIEW_VERSION


def test_129_ordered_columns_bound() -> None:
    fv = _fv()
    assert fv.column_count == 129
    assert [c.index for c in fv.columns] == list(range(129))
    assert len({c.path for c in fv.columns}) == 129


@pytest.mark.parametrize("field", list(_H) + ["epoch", "partition", "row_count"])
def test_any_identity_field_tamper_changes_hash(field: str) -> None:
    base = _fv()
    if field == "epoch":
        other = build_feature_view_manifest(
            epoch=2, partition="train", row_count=3, members=_members(3), **_H
        )
    elif field == "partition":
        other = _fv(partition="validation")
    elif field == "row_count":
        other = build_feature_view_manifest(
            epoch=1, partition="train", row_count=2, members=_members(2), **_H
        )
    else:
        h = dict(_H)
        h[field] = "f" * 64
        other = build_feature_view_manifest(
            epoch=1, partition="train", row_count=3, members=_members(3), **h
        )
    assert other.feature_view_hash != base.feature_view_hash


def test_test_partition_rejected() -> None:
    with pytest.raises(ValueError, match="partition"):
        _fv(partition="test")


def test_reordered_columns_rejected() -> None:
    fs = build_feature_set_manifest()
    cols = [
        FeatureViewColumn(
            index=c.index,
            path=c.path,
            source_schema=c.source_schema,
            state=c.state,
            value_kind=c.value_kind,
        )
        for c in fs.columns
    ]
    swapped = (cols[1], cols[0], *cols[2:])  # indices now 1,0,2,... -> not 0..128
    with pytest.raises(ValueError, match="index"):
        FeatureViewManifest(
            epoch=1,
            partition="train",
            feature_set_hash=fs.feature_set_hash,
            feature_registry_hash=fs.registry_hash,
            column_count=129,
            columns=swapped,
            row_count=0,
            members=(),
            **_H,
        )


def test_forged_feature_view_hash_rejected() -> None:
    data = _fv().model_dump()
    data["feature_view_hash"] = "0" * 64  # wrong hash, re-validated on construction
    with pytest.raises(ValueError, match="feature_view_hash"):
        FeatureViewManifest(**data)


def test_member_count_must_equal_row_count() -> None:
    with pytest.raises(ValueError, match="row_count"):
        build_feature_view_manifest(
            epoch=1, partition="train", row_count=5, members=_members(3), **_H
        )
