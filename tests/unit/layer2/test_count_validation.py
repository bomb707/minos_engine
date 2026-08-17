"""COUNT/REAL/FRACTION value validation and CanonicalFeatureVector hardening (L2-A)."""

from __future__ import annotations

import math
from decimal import Decimal

import pydantic
import pytest

from minos_engine.layer2 import feature_registry as FR
from minos_engine.layer2.contracts import CanonicalFeatureVector

_COUNT = FR.record_for("filter_counts.observed")  # COUNT / CONDITIONAL
_REAL = FR.record_for("mapping_quality.mean")  # REAL / ELIGIBLE
_FRACTION = FR.record_for("reads.mapped_fraction")  # FRACTION / ELIGIBLE
_H = "a" * 64


# --------------------------------------------------------------------------- #
# COUNT accepted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [0, 1, 42, 2**53])
def test_count_accepts_nonnegative_exact_int(value):
    out = FR.validate_scalar_value(_COUNT, value)
    assert out == float(value)
    assert isinstance(out, float)


def test_count_conversion_is_exact_and_deterministic():
    assert FR.validate_scalar_value(_COUNT, 2**53) == 9007199254740992.0
    assert FR.validate_scalar_value(_COUNT, 7) == FR.validate_scalar_value(_COUNT, 7)


# --------------------------------------------------------------------------- #
# COUNT rejected
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        -1,
        0.0,
        1.0,
        1.5,
        -0.5,
        2**53 + 1,
        "1",
        None,
        math.nan,
        math.inf,
        -math.inf,
        Decimal("1"),
    ],
)
def test_count_rejects_invalid(value):
    with pytest.raises(ValueError):
        FR.validate_scalar_value(_COUNT, value)


# --------------------------------------------------------------------------- #
# REAL / FRACTION regression
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [5, 5.5, 0, -3, 0.0])
def test_real_accepts_int_and_float(value):
    assert FR.validate_scalar_value(_REAL, value) == float(value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, False, "5", None])
def test_real_rejects_invalid(value):
    with pytest.raises(ValueError):
        FR.validate_scalar_value(_REAL, value)


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0, 0.5])
def test_fraction_boundaries_accepted(value):
    assert 0.0 <= FR.validate_scalar_value(_FRACTION, value) <= 1.0


@pytest.mark.parametrize("value", [-0.001, 1.001, -1, 2, math.nan, math.inf, True])
def test_fraction_out_of_range_or_invalid_rejected(value):
    with pytest.raises(ValueError):
        FR.validate_scalar_value(_FRACTION, value)


def test_non_scalar_kind_rejected():
    container = FR.record_for("mapping_quality.quantiles")  # CONTAINER
    with pytest.raises(ValueError):
        FR.validate_scalar_value(container, 1.0)


# --------------------------------------------------------------------------- #
# Mapping-level integration (only ELIGIBLE fields reach the mapping)
# --------------------------------------------------------------------------- #
def test_mapping_rejects_bool_nan_inf_and_type():
    for bad in (True, math.nan, math.inf, "0.9", None, Decimal("0.9")):
        with pytest.raises(ValueError):
            FR.validate_production_feature_mapping({"reads.mapped_fraction": bad})


def test_mapping_order_independent_and_deterministic():
    a = FR.validate_production_feature_mapping(
        {"reads.mapped_fraction": 0.9, "mapping_quality.mean": 42.0}
    )
    b = FR.validate_production_feature_mapping(
        {"mapping_quality.mean": 42.0, "reads.mapped_fraction": 0.9}
    )
    assert a.fields == b.fields
    assert a.values == b.values
    assert a.vector_hash == b.vector_hash


def test_mapping_value_change_changes_vector_hash():
    a = FR.validate_production_feature_mapping({"reads.mapped_fraction": 0.9})
    b = FR.validate_production_feature_mapping({"reads.mapped_fraction": 0.8})
    assert a.vector_hash != b.vector_hash


def test_mapping_field_change_changes_vector_hash():
    a = FR.validate_production_feature_mapping({"reads.mapped_fraction": 0.9})
    b = FR.validate_production_feature_mapping({"reads.unmapped_fraction": 0.9})
    assert a.vector_hash != b.vector_hash


# --------------------------------------------------------------------------- #
# CanonicalFeatureVector direct-construction hardening
# --------------------------------------------------------------------------- #
def test_cv_rejects_nan():
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("a",), values=(math.nan,), registry_hash=_H)


def test_cv_rejects_infinity():
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("a",), values=(math.inf,), registry_hash=_H)
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("a",), values=(-math.inf,), registry_hash=_H)


def test_cv_rejects_bool():
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("a",), values=(True,), registry_hash=_H)


def test_cv_rejects_unsorted_fields():
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("b", "a"), values=(1.0, 2.0), registry_hash=_H)


def test_cv_rejects_duplicate_fields():
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("a", "a"), values=(1.0, 2.0), registry_hash=_H)


def test_cv_rejects_length_mismatch():
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("a",), values=(1.0, 2.0), registry_hash=_H)


def test_cv_rejects_malformed_registry_hash():
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("a",), values=(1.0,), registry_hash="short")


def test_cv_rejects_incorrect_supplied_vector_hash():
    with pytest.raises(pydantic.ValidationError):
        CanonicalFeatureVector(fields=("a",), values=(1.0,), registry_hash=_H, vector_hash="b" * 64)


def test_cv_accepts_valid_and_binds_hash():
    cv = CanonicalFeatureVector(fields=("a", "b"), values=(1.0, 2.0), registry_hash=_H)
    assert len(cv.vector_hash) == 64
    # supplying the correct vector hash round-trips
    cv2 = CanonicalFeatureVector(
        fields=("a", "b"), values=(1.0, 2.0), registry_hash=_H, vector_hash=cv.vector_hash
    )
    assert cv2.vector_hash == cv.vector_hash
