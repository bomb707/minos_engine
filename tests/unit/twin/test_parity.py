"""Test group F — parity assessment and declared-level enforcement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.twin.contracts import (
    DECLARED_PARITY_LEVEL,
    ParityDifferenceKind,
    ParityExpectation,
    ParityLevel,
    ParityObservation,
    TwinParityReport,
)
from minos_engine.twin.parity import assess_parity

_H = "a" * 64
_TS = "2026-08-17T12:00:00+00:00"


def _assess(expectation, observation):
    return assess_parity(
        name="p",
        expectation=expectation,
        observation=observation,
        declared_level=DECLARED_PARITY_LEVEL,
        created_at=_TS,
    )


def test_exact_match():
    r = _assess(
        ParityExpectation(name="p", expected_hash=_H, fields={"caller": "gatk"}),
        ParityObservation(name="p", observed_hash=_H, fields={"caller": "gatk"}),
    )
    assert r.matched and not r.differences


def test_hash_mismatch():
    r = _assess(
        ParityExpectation(name="p", expected_hash=_H),
        ParityObservation(name="p", observed_hash="b" * 64),
    )
    assert not r.matched
    assert r.differences[0].kind is ParityDifferenceKind.HASH_MISMATCH


def test_missing_expected_field():
    r = _assess(
        ParityExpectation(name="p", fields={"k": "v"}),
        ParityObservation(name="p", fields={}),
    )
    assert any(d.kind is ParityDifferenceKind.MISSING_EXPECTED for d in r.differences)


def test_unexpected_field():
    r = _assess(
        ParityExpectation(name="p", fields={}),
        ParityObservation(name="p", fields={"extra": "1"}),
    )
    assert any(d.kind is ParityDifferenceKind.UNEXPECTED for d in r.differences)


def test_value_tool_and_protocol_mismatch():
    r = _assess(
        ParityExpectation(name="p", fields={"k": "a"}, tool_version="1", protocol_version="v1"),
        ParityObservation(name="p", fields={"k": "b"}, tool_version="2", protocol_version="v2"),
    )
    kinds = {d.kind for d in r.differences}
    assert ParityDifferenceKind.VALUE_MISMATCH in kinds
    assert ParityDifferenceKind.TOOL_VERSION_MISMATCH in kinds
    assert ParityDifferenceKind.PROTOCOL_VERSION_MISMATCH in kinds


def test_declared_level_carried_verbatim():
    r = _assess(
        ParityExpectation(name="p", expected_hash=_H),
        ParityObservation(name="p", observed_hash=_H),
    )
    assert r.declared_level is ParityLevel.FIXTURE_REPLAY


def test_matched_report_cannot_have_differences():
    from minos_engine.twin.contracts import ParityDifference

    with pytest.raises(ValidationError):
        TwinParityReport(
            name="p",
            declared_level=ParityLevel.FIXTURE_REPLAY,
            matched=True,
            differences=(ParityDifference(field="x", kind=ParityDifferenceKind.HASH_MISMATCH),),
            created_at=_TS,
        )


def test_mismatched_report_requires_differences():
    with pytest.raises(ValidationError):
        TwinParityReport(
            name="p",
            declared_level=ParityLevel.FIXTURE_REPLAY,
            matched=False,
            differences=(),
            created_at=_TS,
        )
