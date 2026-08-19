"""E1 contracts: manifest derivation, frozen hashes, fail-closed value/order rules."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.layer2.features.contracts import (
    FeatureColumn,
    FeatureMatrix,
    FeatureSetManifest,
    FeatureVector,
    MatrixMember,
    build_feature_set_manifest,
    validate_value_for_kind,
)
from minos_engine.schema_registry import validate_against

_H = "a" * 64
_MANIFEST = build_feature_set_manifest()
_FROZEN_SET_HASH = "7e867dfa5633044b69869be8a87fac564431a73a183aa0ab0b1b13158a7c176f"


def _vector(**overrides):
    base = {
        "epoch": 1,
        "dataset_id": "minos-chr18-0000000000000001",
        "profile_id": "p1",
        "content_hash": "b" * 64,
        "feature_values_hash": "c" * 64,
        "partition": "train",
        "snapshot_hash": "d" * 64,
        "registry_hash": _MANIFEST.registry_hash,
        "feature_set_hash": _MANIFEST.feature_set_hash,
        "value_count": 129,
        "values": tuple(0.5 for _ in range(129)),
    }
    base.update(overrides)
    return FeatureVector(**base)


# --------------------------------------------------------------------------- #
# manifest (item 1)
# --------------------------------------------------------------------------- #
def test_manifest_is_exactly_129_sorted_contiguous() -> None:
    assert _MANIFEST.column_count == 129 and len(_MANIFEST.columns) == 129
    paths = [c.path for c in _MANIFEST.columns]
    assert paths == sorted(paths) and len(set(paths)) == 129
    assert [c.index for c in _MANIFEST.columns] == list(range(129))
    assert all(
        c.source_schema == "bam-profile-v1" and c.state == "ELIGIBLE" for c in _MANIFEST.columns
    )


def test_feature_set_hash_is_frozen_and_stable() -> None:
    assert _MANIFEST.feature_set_hash == _FROZEN_SET_HASH
    assert build_feature_set_manifest().feature_set_hash == _FROZEN_SET_HASH


def test_registry_drift_rejected() -> None:
    with pytest.raises(ValidationError, match="registry"):
        FeatureSetManifest(registry_hash="0" * 64, column_count=129, columns=_MANIFEST.columns)


def _mutated_columns(mutate):
    cols = list(_MANIFEST.columns)
    mutate(cols)
    return tuple(cols)


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("missing", lambda c: c.pop(0)),
        ("duplicate", lambda c: c.__setitem__(1, c[0].model_copy(update={"index": 1}))),
        ("reordered", lambda c: c.append(c.pop(0))),
        ("index_gap", lambda c: c.__setitem__(5, c[5].model_copy(update={"index": 99}))),
    ],
)
def test_column_manifest_mutations_rejected(name, mutate) -> None:
    with pytest.raises(ValidationError):
        FeatureSetManifest(
            registry_hash=_MANIFEST.registry_hash,
            column_count=129,
            columns=_mutated_columns(mutate),
        )


def test_wrong_state_schema_kind_rejected() -> None:
    good = _MANIFEST.columns[0]
    with pytest.raises(ValidationError):
        FeatureColumn(**{**good.model_dump(), "source_schema": "window-profile-v1"})
    with pytest.raises(ValidationError):
        FeatureColumn(**{**good.model_dump(), "state": "RESEARCH_ONLY"})
    with pytest.raises(ValidationError):
        FeatureColumn(**{**good.model_dump(), "value_kind": "STRING"})
    # kind mismatch vs registry
    flipped = "FRACTION" if good.value_kind == "REAL" else "REAL"
    cols = (good.model_copy(update={"value_kind": flipped}),) + _MANIFEST.columns[1:]
    with pytest.raises(ValidationError, match="value_kind mismatch"):
        FeatureSetManifest(registry_hash=_MANIFEST.registry_hash, column_count=129, columns=cols)


# --------------------------------------------------------------------------- #
# value validation (item 2)
# --------------------------------------------------------------------------- #
def test_value_kind_boundaries() -> None:
    assert validate_value_for_kind("REAL", 1.5, "p") == 1.5
    assert validate_value_for_kind("FRACTION", 1.0, "p") == 1.0
    assert validate_value_for_kind("COUNT", 7, "p") == 7.0
    for kind, bad in (
        ("REAL", float("nan")),
        ("REAL", float("inf")),
        ("REAL", True),
        ("FRACTION", 1.0001),
        ("FRACTION", -0.1),
        ("FRACTION", None),
        ("COUNT", 1.5),
        ("COUNT", -1),
        ("COUNT", 2**53 + 1),
        ("COUNT", False),
    ):
        with pytest.raises(ValueError):
            validate_value_for_kind(kind, bad, "p")


def test_vector_rejects_nonfinite_and_wrong_length() -> None:
    with pytest.raises(ValidationError):
        _vector(values=tuple([float("nan")] + [0.5] * 128))
    with pytest.raises(ValidationError, match="wrong-length"):
        _vector(values=tuple(0.5 for _ in range(128)))  # value_count says 129


def test_vector_validate_against_manifest_fraction_bound() -> None:
    v = _vector()
    v.validate_against(_MANIFEST)  # all 0.5 valid for REAL + FRACTION
    frac_idx = next(i for i, c in enumerate(_MANIFEST.columns) if c.value_kind == "FRACTION")
    bad_vals = list(v.values)
    bad_vals[frac_idx] = 1.5
    bad = _vector(values=tuple(bad_vals))
    with pytest.raises(ValueError, match="FRACTION"):
        bad.validate_against(_MANIFEST)


# --------------------------------------------------------------------------- #
# hashes + ordering (items 3-4)
# --------------------------------------------------------------------------- #
def test_vector_binding_mutations_change_vector_hash() -> None:
    base = _vector().vector_hash
    assert _vector(epoch=2).vector_hash != base
    assert _vector(partition="validation").vector_hash != base
    assert _vector(content_hash="e" * 64).vector_hash != base
    vals = [0.5] * 129
    vals[3] = 0.25
    assert _vector(values=tuple(vals)).vector_hash != base


def test_tampered_vector_hash_rejected() -> None:
    raw = _vector().model_dump()
    raw["vector_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="vector_hash"):
        FeatureVector(**raw)


def _matrix(members, **overrides):
    base = {
        "epoch": 1,
        "snapshot_hash": "d" * 64,
        "partition": "train",
        "registry_hash": _MANIFEST.registry_hash,
        "feature_set_hash": _MANIFEST.feature_set_hash,
        "row_count": len(members),
        "column_count": 129,
        "members": tuple(members),
    }
    base.update(overrides)
    return FeatureMatrix(**base)


def _members(n):
    return [MatrixMember(dataset_id=f"ds-{i:04d}", vector_hash="f" * 64) for i in range(n)]


def test_matrix_member_mutation_changes_hash() -> None:
    a = _matrix(_members(4))
    swapped = _members(4)
    swapped[2] = MatrixMember(dataset_id=swapped[2].dataset_id, vector_hash="e" * 64)
    assert _matrix(swapped).matrix_hash != a.matrix_hash


def test_matrix_reordering_and_duplicates_rejected() -> None:
    ms = _members(4)
    with pytest.raises(ValidationError, match="ordered"):
        _matrix([ms[1], ms[0], ms[2], ms[3]], row_count=4)
    with pytest.raises(ValidationError, match="duplicate"):
        _matrix([ms[0], ms[0], ms[2], ms[3]], row_count=4)


def test_test_partition_rejected_everywhere() -> None:
    with pytest.raises(ValidationError):
        _vector(partition="test")
    with pytest.raises(ValidationError):
        _matrix(_members(2), partition="test")


def test_matrix_counts_derive_from_members_not_constants() -> None:
    # arbitrary non-50/10 sizes work: counts derive from membership (no fixed 50/10/15).
    for n in (3, 7, 61):
        m = _matrix(_members(n))
        assert m.row_count == n


# --------------------------------------------------------------------------- #
# schema/runtime parity (item 5)
# --------------------------------------------------------------------------- #
def test_schema_runtime_parity() -> None:
    validate_against("feature-set-v1", _MANIFEST.model_dump(mode="json"))
    validate_against("feature-vector-v1", _vector().model_dump(mode="json"))
    validate_against("feature-matrix-v1", _matrix(_members(3)).model_dump(mode="json"))
    # schema rejects the test partition too
    from minos_engine.common.errors import ContractValidationError

    raw = _matrix(_members(2)).model_dump(mode="json")
    raw["partition"] = "test"
    with pytest.raises(ContractValidationError):
        validate_against("feature-matrix-v1", raw)


def test_select_config_remains_blocked() -> None:
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]
