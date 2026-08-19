"""Attestation contract + canonical feature-values hash: determinism + fail-closed."""

from __future__ import annotations

import pytest

from minos_engine.layer2.ingest.contracts import (
    CANONICAL_FEATURE_VALUES_DOMAIN,
    InputIntegrityAttestation,
    M5Status,
    admission_for_m5,
    canonical_feature_values_hash,
)
from minos_engine.schema_registry import validate_against


def _attestation(**overrides: object) -> InputIntegrityAttestation:
    base: dict[str, object] = {
        "generator": "minos-engine intake attest-input",
        "generator_version": "l2d-attest-v1",
        "dataset_id": "minos-chr18-0123456789abcdef",
        "round_id": "0123456789abcdef",
        "chromosome": "chr18",
        "registry_snapshot_hash": "a" * 64,
        "bam_sha256": "b" * 64,
        "bai_sha256": "c" * 64,
        "reference_sha256": "d" * 64,
        "fai_sha256": "e" * 64,
        "region_hash": "f" * 64,
        "identity_tuple_hash": "1" * 64,
        "bam_sq_m5": "2" * 32,
        "computed_reference_m5": "2" * 32,
        "m5_status": M5Status.MATCH,
    }
    base.update(overrides)
    return InputIntegrityAttestation.model_validate(base)


def test_attestation_hash_deterministic_and_schema_valid() -> None:
    a = _attestation()
    validate_against("input-integrity-attestation-v1", a.model_dump(mode="json"))
    b = InputIntegrityAttestation.model_validate(a.model_dump(mode="json"))
    assert a.attestation_hash == b.attestation_hash


def test_m5_status_must_be_consistent() -> None:
    with pytest.raises(ValueError):
        _attestation(m5_status=M5Status.ABSENT)  # tag present -> ABSENT inconsistent
    with pytest.raises(ValueError):
        _attestation(bam_sq_m5=None, m5_status=M5Status.MATCH)
    with pytest.raises(ValueError):
        _attestation(bam_sq_m5="3" * 32, m5_status=M5Status.MATCH)  # actually MISMATCH


def test_mismatch_and_absent_statuses_derive() -> None:
    assert _attestation(bam_sq_m5=None, m5_status=M5Status.ABSENT).m5_status is M5Status.ABSENT
    m = _attestation(bam_sq_m5="3" * 32, m5_status=M5Status.MISMATCH)
    assert m.m5_status is M5Status.MISMATCH


def test_tampered_attestation_hash_rejected() -> None:
    raw = _attestation().model_dump(mode="json")
    raw["attestation_hash"] = "0" * 64
    with pytest.raises(ValueError):
        InputIntegrityAttestation.model_validate(raw)


def test_admission_matrix() -> None:
    assert admission_for_m5(M5Status.MATCH) == (True, False)
    assert admission_for_m5(M5Status.ABSENT) == (True, True)
    assert admission_for_m5(M5Status.MISMATCH) == (False, False)


def test_feature_hash_order_independent_and_domain_separated() -> None:
    h1 = canonical_feature_values_hash({"a.b": 1, "c.d": 2.5})
    h2 = canonical_feature_values_hash({"c.d": 2.5, "a.b": 1})
    assert h1 == h2
    # domain separation: the same payload hashed without the domain differs.
    import hashlib

    from minos_engine.common.canonical_json import canonical_json_str

    bare = hashlib.sha256(canonical_json_str({"a.b": 1, "c.d": 2.5}).encode()).hexdigest()
    assert h1 != bare
    assert CANONICAL_FEATURE_VALUES_DOMAIN == "minos:canonical-feature-values:v1"


def test_feature_hash_type_sensitive() -> None:
    # canonical JSON preserves int vs float — 1 and 1.0 are different content.
    assert canonical_feature_values_hash({"x": 1}) != canonical_feature_values_hash({"x": 1.0})
