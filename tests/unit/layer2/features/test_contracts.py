"""E1 contracts: authoritative manifest, fail-closed vectors, frozen hashes, parity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.common.errors import ContractValidationError
from minos_engine.layer2.features.contracts import (
    AUTHORITATIVE_COLUMNS,
    FEATURE_SET_DOMAIN,
    FROZEN_FEATURE_SET_HASH,
    FeatureColumn,
    FeatureMatrix,
    FeatureSetManifest,
    FeatureVector,
    MatrixMember,
    _domain_hash,
    build_feature_set_manifest,
    validate_value_for_kind,
)
from minos_engine.schema_registry import validate_against

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


# --------------------------------------------------------------------------- #
# manifest = the COMPLETE authoritative set (item 1)
# --------------------------------------------------------------------------- #
def test_manifest_is_exactly_the_authoritative_129() -> None:
    assert len(AUTHORITATIVE_COLUMNS) == 129
    assert _MANIFEST.column_count == 129 and len(_MANIFEST.columns) == 129
    paths = [c.path for c in _MANIFEST.columns]
    assert paths == list(AUTHORITATIVE_COLUMNS)
    assert paths == sorted(paths) and len(set(paths)) == 129
    assert [c.index for c in _MANIFEST.columns] == list(range(129))
    assert all(
        c.source_schema == "bam-profile-v1" and c.state == "ELIGIBLE" for c in _MANIFEST.columns
    )


def test_feature_set_hash_is_frozen_and_stable() -> None:
    assert _MANIFEST.feature_set_hash == _FROZEN_SET_HASH == FROZEN_FEATURE_SET_HASH
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


def test_incomplete_set_with_matching_count_and_recomputed_hash_rejected() -> None:
    """Drop one column, fix column_count to 128 AND recompute a self-consistent hash:
    still rejected — specifically because the authoritative set is incomplete."""
    dropped = tuple(c.model_copy(update={"index": i}) for i, c in enumerate(_MANIFEST.columns[1:]))
    recomputed = _domain_hash(
        FEATURE_SET_DOMAIN,
        {
            "schema_version": "feature-set-v1",
            "registry_hash": _MANIFEST.registry_hash,
            "column_count": 128,
            "columns": [
                {
                    "index": c.index,
                    "path": c.path,
                    "source_schema": c.source_schema,
                    "state": c.state,
                    "value_kind": c.value_kind,
                }
                for c in dropped
            ],
        },
    )
    with pytest.raises(ValidationError, match="authoritative"):
        FeatureSetManifest(
            registry_hash=_MANIFEST.registry_hash,
            column_count=128,
            columns=dropped,
            feature_set_hash=recomputed,
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
# fail-closed vector construction (item 2)
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
    with pytest.raises(ValidationError):
        _vector(values=tuple([float("inf")] + [0.5] * 128))
    with pytest.raises(ValidationError, match="wrong-length"):
        _vector(values=tuple(0.5 for _ in range(128)))  # value_count says 129


def test_vector_rejects_bool_and_numeric_string_values() -> None:
    with pytest.raises(ValidationError, match="non-bool number"):
        _vector(values=(True,) + tuple(0.5 for _ in range(128)))
    with pytest.raises(ValidationError, match="no coercion"):
        _vector(values=("0.5",) + tuple(0.5 for _ in range(128)))
    with pytest.raises(ValidationError, match="no coercion"):
        _vector(epoch="1")
    with pytest.raises(ValidationError, match="no coercion"):
        _vector(value_count="129")


def test_vector_rejects_128_and_130_values_even_internally_consistent() -> None:
    with pytest.raises(ValidationError, match="exactly 129"):
        _vector(value_count=128, values=tuple(0.5 for _ in range(128)))
    with pytest.raises(ValidationError, match="exactly 129"):
        _vector(value_count=130, values=tuple(0.5 for _ in range(130)))


def test_vector_fraction_enforced_at_construction() -> None:
    v = _vector()  # all 0.5 valid for REAL + FRACTION
    assert v.value_count == 129
    frac_idx = next(i for i, c in enumerate(_MANIFEST.columns) if c.value_kind == "FRACTION")
    bad_vals = list(v.values)
    bad_vals[frac_idx] = 1.5
    # direct construction must not produce a hash for an invalid vector.
    with pytest.raises(ValidationError, match="FRACTION"):
        _vector(values=tuple(bad_vals))


# --------------------------------------------------------------------------- #
# registry / frozen-set / count bindings (items 3-4) + strict hex (item 5)
# --------------------------------------------------------------------------- #
def test_vector_and_matrix_bind_registry_frozen_set_and_129() -> None:
    with pytest.raises(ValidationError, match="accepted feature registry"):
        _vector(registry_hash="0" * 64)
    with pytest.raises(ValidationError, match="frozen FEATURE-READY-v1"):
        _vector(feature_set_hash="0" * 64)
    with pytest.raises(ValidationError, match="accepted feature registry"):
        _matrix(_members(2), registry_hash="0" * 64)
    with pytest.raises(ValidationError, match="frozen FEATURE-READY-v1"):
        _matrix(_members(2), feature_set_hash="0" * 64)
    with pytest.raises(ValidationError, match="exactly 129"):
        _matrix(_members(2), column_count=128)
    with pytest.raises(ValidationError, match="exactly 129"):
        _matrix(_members(2), column_count=130)


def test_hash_fields_strict_lowercase_hex() -> None:
    with pytest.raises(ValidationError):  # nested member hash: uppercase
        MatrixMember(dataset_id="d", vector_hash="F" * 64)
    with pytest.raises(ValidationError):  # nested member hash: non-hex
        MatrixMember(dataset_id="d", vector_hash="z" * 64)
    with pytest.raises(ValidationError, match="lowercase 64-hex"):
        _vector(content_hash="B" * 64)
    with pytest.raises(ValidationError, match="lowercase 64-hex"):
        _vector(snapshot_hash="z" * 64)
    with pytest.raises(ValidationError, match="lowercase 64-hex"):
        _matrix(_members(2), snapshot_hash="Z" * 64)
    good = _vector()
    with pytest.raises(ValidationError, match="lowercase 64-hex"):
        FeatureVector(**{**good.model_dump(), "vector_hash": good.vector_hash.upper()})


# --------------------------------------------------------------------------- #
# hashes + ordering
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


def test_artifact_sha256_stays_outside_logical_matrix() -> None:
    m = _matrix(_members(2))
    assert "artifact_sha256" not in m.model_dump()
    # the byte-artifact identity can NEVER enter the logical matrix (extra=forbid).
    with pytest.raises(ValidationError):
        _matrix(_members(2), artifact_sha256="a" * 64)


def test_nested_extra_properties_rejected_at_runtime() -> None:
    with pytest.raises(ValidationError):
        MatrixMember.model_validate({"dataset_id": "d", "vector_hash": "f" * 64, "extra": 1})
    with pytest.raises(ValidationError):
        FeatureColumn.model_validate({**_MANIFEST.columns[0].model_dump(), "extra": 1})


# --------------------------------------------------------------------------- #
# schema/runtime parity (items 6-7)
# --------------------------------------------------------------------------- #
def test_schema_runtime_parity() -> None:
    validate_against("feature-set-v1", _MANIFEST.model_dump(mode="json"))
    validate_against("feature-vector-v1", _vector().model_dump(mode="json"))
    validate_against("feature-matrix-v1", _matrix(_members(3)).model_dump(mode="json"))
    # schema rejects the test partition too
    raw = _matrix(_members(2)).model_dump(mode="json")
    raw["partition"] = "test"
    with pytest.raises(ContractValidationError):
        validate_against("feature-matrix-v1", raw)


def _schema_rejects(name: str, raw: dict) -> None:
    with pytest.raises(ContractValidationError):
        validate_against(name, raw)


def test_schema_parity_negative_vector_cases() -> None:
    vec = _vector().model_dump(mode="json")
    for tweak in (
        {"values": [True] + vec["values"][1:]},  # bool value
        {"values": ["0.5"] + vec["values"][1:]},  # numeric-string value
        {"values": vec["values"][:128], "value_count": 128},  # 128 values
        {"values": vec["values"] + [0.5], "value_count": 130},  # 130 values
        {"registry_hash": "0" * 64},  # wrong accepted registry hash
        {"feature_set_hash": "0" * 64},  # wrong frozen feature_set_hash
        {"content_hash": "B" * 64},  # uppercase hash
        {"content_hash": "zz" * 32},  # non-hex hash
        {"nested": {"extra": 1}},  # extra property
    ):
        _schema_rejects("feature-vector-v1", {**vec, **tweak})


def test_schema_parity_negative_matrix_and_set_cases() -> None:
    mat = _matrix(_members(3)).model_dump(mode="json")
    for tweak in (
        {"registry_hash": "0" * 64},
        {"feature_set_hash": "0" * 64},
        {"column_count": 128},
        {"members": [{**mat["members"][0], "vector_hash": "F" * 64}] + mat["members"][1:]},
        {"members": [{**mat["members"][0], "extra": 1}] + mat["members"][1:]},
    ):
        _schema_rejects("feature-matrix-v1", {**mat, **tweak})
    ms = _MANIFEST.model_dump(mode="json")
    for tweak in (
        # incomplete 128-column set with internally matching column_count
        {"column_count": 128, "columns": ms["columns"][:128]},
        {"registry_hash": "0" * 64},
        {"feature_set_hash": "0" * 64},
        {"columns": [{**ms["columns"][0], "extra": 1}] + ms["columns"][1:]},
    ):
        _schema_rejects("feature-set-v1", {**ms, **tweak})


def test_select_config_remains_blocked() -> None:
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]
