"""Admission validation: frozen L1 pin, ELIGIBLE extraction fail-closed, decision shape."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import ContractValidationError
from minos_engine.layer2.ingest.contracts import extract_eligible_feature_values
from minos_engine.layer2.ingest.validation import (
    L1_IDENTITY_SECTIONS,
    l1_feature_values_hash_from_document,
)


def test_l1_section_pin_matches_layer1_fingerprint() -> None:
    # Tests may import layer1 internals; the production layer2 package may not.
    from minos_engine.layer1.fingerprint import _IDENTITY_SECTIONS

    assert L1_IDENTITY_SECTIONS == _IDENTITY_SECTIONS


def test_l1_hash_matches_layer1_formula_on_synthetic_document() -> None:
    doc = {s: {"v": i} for i, s in enumerate(L1_IDENTITY_SECTIONS)}
    from minos_engine.common.hashing import canonical_hash

    assert l1_feature_values_hash_from_document(doc) == canonical_hash(
        {s: doc[s] for s in L1_IDENTITY_SECTIONS}
    )


def test_l1_hash_missing_section_fails_closed() -> None:
    from minos_engine.common.errors import AdmissionRejectedError

    with pytest.raises(AdmissionRejectedError):
        l1_feature_values_hash_from_document({"reads": {}})


def test_eligible_extraction_missing_field_fails_closed() -> None:
    with pytest.raises(ContractValidationError):
        extract_eligible_feature_values({})  # every ELIGIBLE field missing


def test_eligible_extraction_null_field_fails_closed() -> None:
    from minos_engine.layer2.feature_registry import production_eligible_fields

    # Build a document where the FIRST eligible path exists but is null.
    path = production_eligible_fields()[0]
    doc: dict = {}
    node = doc
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = None
    with pytest.raises(ContractValidationError):
        extract_eligible_feature_values(doc)
