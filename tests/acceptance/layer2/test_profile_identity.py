"""Mandatory Layer 1 profile identities and the derived identity-tuple hash."""

from __future__ import annotations

import pydantic
import pytest

from minos_engine.layer2.contracts import Layer1ProfileReference

A = "a" * 64
B = "b" * 64


def _ref(**over) -> Layer1ProfileReference:
    kwargs = {
        "profile_id": "p1",
        "profile_manifest_hash": A,
        "fingerprint_hash": A,
        "region_hash": A,
        "bam_sha256": A,
        "bai_sha256": A,
        "reference_sha256": A,
        "fai_sha256": A,
    }
    kwargs.update(over)
    return Layer1ProfileReference(**kwargs)


def test_all_eight_fields_accepted():
    ref = _ref()
    assert ref.identity_tuple_hash and len(ref.identity_tuple_hash) == 64


@pytest.mark.parametrize("drop", ["bai_sha256", "reference_sha256", "fai_sha256", "region_hash"])
def test_missing_identity_rejected(drop):
    kwargs = {
        "profile_id": "p1",
        "profile_manifest_hash": A,
        "fingerprint_hash": A,
        "region_hash": A,
        "bam_sha256": A,
        "bai_sha256": A,
        "reference_sha256": A,
        "fai_sha256": A,
    }
    kwargs.pop(drop)
    with pytest.raises(pydantic.ValidationError):
        Layer1ProfileReference(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "short", "A" * 64, "g" * 64])
def test_malformed_or_uppercase_hash_rejected(bad):
    with pytest.raises(pydantic.ValidationError):
        _ref(bai_sha256=bad)


def test_identity_tuple_hash_deterministic():
    assert _ref().identity_tuple_hash == _ref().identity_tuple_hash


@pytest.mark.parametrize(
    "component", ["bam_sha256", "bai_sha256", "reference_sha256", "fai_sha256", "region_hash"]
)
def test_component_change_alters_tuple_hash(component):
    base = _ref().identity_tuple_hash
    changed = _ref(**{component: B}).identity_tuple_hash
    assert changed != base


def test_supplied_tuple_hash_must_match():
    with pytest.raises(pydantic.ValidationError):
        _ref(identity_tuple_hash="c" * 64)
