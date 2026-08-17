"""Canonical JSON: determinism, key-order invariance, type sensitivity, finite-only."""

from __future__ import annotations

import math

import pytest

from minos_engine.common.canonical_json import canonical_json_bytes, canonical_json_str
from minos_engine.common.errors import CanonicalizationError
from minos_engine.common.hashing import canonical_hash, sha256_hex


def test_key_order_does_not_change_bytes_or_hash():
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert canonical_json_bytes(a) == canonical_json_bytes(b)
    assert canonical_hash(a) == canonical_hash(b)


def test_missing_key_changes_hash():
    assert canonical_hash({"a": 1, "b": 2}) != canonical_hash({"a": 1})


def test_type_changes_hash():
    assert canonical_hash({"a": 1}) != canonical_hash({"a": "1"})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 1.0})
    assert canonical_hash({"a": True}) != canonical_hash({"a": 1})


def test_bool_and_int_preserved():
    assert canonical_json_str({"a": True}) == '{"a":true}'
    assert canonical_json_str({"a": 1}) == '{"a":1}'
    assert canonical_json_str({"a": 1.0}) == '{"a":1.0}'


def test_compact_sorted_separators():
    assert canonical_json_str({"b": 1, "a": 2}) == '{"a":2,"b":1}'


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nan_infinity_rejected(bad):
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"x": bad})


def test_non_string_keys_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({1: "a"})


def test_repeated_execution_is_stable():
    value = {"nested": [1, 2, {"k": "v"}], "flag": False, "n": 3.5}
    first = canonical_json_bytes(value)
    for _ in range(5):
        assert canonical_json_bytes(value) == first
        assert sha256_hex(first) == canonical_hash(value)
