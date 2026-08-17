"""Deterministic parity assessment between an expectation and an observation.

Produces a ``TwinParityReport`` at a declared parity level. A lower level is
never reported as a higher level: ``declared_level`` is carried verbatim and
enforced by the contract and tests. Numeric tolerance is applied only when it is
authoritative; Stage 1 uses exact hash/field equality (FIXTURE_REPLAY).
"""

from __future__ import annotations

from .contracts import (
    ParityDifference,
    ParityDifferenceKind,
    ParityExpectation,
    ParityLevel,
    ParityObservation,
    TwinParityReport,
)

__all__ = ["assess_parity"]


def assess_parity(
    *,
    name: str,
    expectation: ParityExpectation,
    observation: ParityObservation,
    declared_level: ParityLevel,
    created_at: str,
) -> TwinParityReport:
    differences: list[ParityDifference] = []

    _has_hash = expectation.expected_hash is not None or observation.observed_hash is not None
    if _has_hash and expectation.expected_hash != observation.observed_hash:
        differences.append(
            ParityDifference(
                field="content_hash",
                kind=ParityDifferenceKind.HASH_MISMATCH,
                expected=expectation.expected_hash,
                observed=observation.observed_hash,
            )
        )

    for key, expected_value in expectation.fields.items():
        if key not in observation.fields:
            differences.append(
                ParityDifference(
                    field=key,
                    kind=ParityDifferenceKind.MISSING_EXPECTED,
                    expected=expected_value,
                    observed=None,
                )
            )
        elif observation.fields[key] != expected_value:
            differences.append(
                ParityDifference(
                    field=key,
                    kind=ParityDifferenceKind.VALUE_MISMATCH,
                    expected=expected_value,
                    observed=observation.fields[key],
                )
            )
    for key in observation.fields:
        if key not in expectation.fields:
            differences.append(
                ParityDifference(
                    field=key,
                    kind=ParityDifferenceKind.UNEXPECTED,
                    expected=None,
                    observed=observation.fields[key],
                )
            )

    if expectation.tool_version != observation.tool_version:
        differences.append(
            ParityDifference(
                field="tool_version",
                kind=ParityDifferenceKind.TOOL_VERSION_MISMATCH,
                expected=expectation.tool_version,
                observed=observation.tool_version,
            )
        )
    if expectation.protocol_version != observation.protocol_version:
        differences.append(
            ParityDifference(
                field="protocol_version",
                kind=ParityDifferenceKind.PROTOCOL_VERSION_MISMATCH,
                expected=expectation.protocol_version,
                observed=observation.protocol_version,
            )
        )

    return TwinParityReport(
        name=name,
        declared_level=declared_level,
        matched=not differences,
        differences=tuple(differences),
        created_at=created_at,
    )
