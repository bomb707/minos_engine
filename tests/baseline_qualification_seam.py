"""TEST-ONLY mint seam for BASELINE-QUALIFIED.

Production source deliberately offers no way to wrap an arbitrary
``BaselineQualificationResult`` in a ``TrustedBaselineQualification`` — that was the defect this
architecture exists to remove. Unit tests still need trusted positives built from controlled
fixtures rather than from real evidence, so the seam lives HERE, under ``tests/``, where it cannot
be reached by anything shipped.

A guard test asserts that no module under ``src/`` provides an equivalent.
"""

from __future__ import annotations

from minos_engine.qualification.l2f2_baseline_qualified_contract import (
    BaselineQualificationResult,
)
from minos_engine.qualification.l2f2_baseline_qualified_qualifier import (
    _MINT,
    TrustedBaselineQualification,
)

__all__ = ["trusted_for_tests"]


def trusted_for_tests(result: BaselineQualificationResult) -> TrustedBaselineQualification:
    """Mint a trusted qualification from a fixture result. Tests only."""
    return TrustedBaselineQualification(_MINT, result)
