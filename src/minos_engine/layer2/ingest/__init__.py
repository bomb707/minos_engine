"""L2-D Layer 1 profile ingestion — pure Layer 2 domain (no DB, no file I/O).

This package owns the ingestion *contracts* and *validation* only. Database writes live
in ``storage.profile_ingest``; the input-integrity attestation is produced by the
``intake`` package (which imports the contract from here — the consumer owns the
contract, so Layer 2 never imports intake). Architecture boundaries are enforced by
``tests/leakage/test_architecture_boundaries.py``: this package imports no pysam, no
intake, no SQLAlchemy, and from Layer 1 only the typed ``layer1.contracts``.
"""

from __future__ import annotations

from .contracts import (
    ATTESTATION_SCHEMA_VERSION,
    CANONICAL_FEATURE_VALUES_DOMAIN,
    InputIntegrityAttestation,
    M5Status,
    admission_for_m5,
    canonical_feature_values_hash,
    extract_eligible_feature_values,
)

__all__ = [
    "ATTESTATION_SCHEMA_VERSION",
    "CANONICAL_FEATURE_VALUES_DOMAIN",
    "InputIntegrityAttestation",
    "M5Status",
    "admission_for_m5",
    "canonical_feature_values_hash",
    "extract_eligible_feature_values",
]
