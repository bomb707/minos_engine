"""Required-check enforcement by gate type (item 6) and integrity/promotion (item 7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import require_gate_pass, verify_gate_integrity

TS = "2026-08-17T12:00:00+00:00"
_EV = (EvidenceItem(description="e", path="reports/x.md", sha256="a" * 64),)


def _protocol_checks(**overrides) -> dict[str, bool]:
    checks = dict.fromkeys(required_checks_for("PROTOCOL-READY"), True)
    checks.update(overrides)
    return checks


def _pass_protocol_gate(mandatory_checks) -> GateArtifact:
    return GateArtifact(
        gate_name="PROTOCOL-READY",
        status=GateStatus.PASS,
        engine_git_sha="abc",
        mandatory_checks=mandatory_checks,
        evidence=_EV,
        qualified_source_git_sha="s",
        qualified_source_tree_sha="t",
        qualification_tool_version="v",
        created_at=TS,
    )


def test_arbitrary_dict_cannot_pass():
    with pytest.raises(ValidationError):
        _pass_protocol_gate({"anything": True})


def test_missing_one_required_check_cannot_pass():
    checks = _protocol_checks()
    checks.pop("mypy_pass")
    with pytest.raises(ValidationError):
        _pass_protocol_gate(checks)


def test_required_check_false_cannot_pass():
    with pytest.raises(ValidationError):
        _pass_protocol_gate(_protocol_checks(coverage_threshold_met=False))


def test_unknown_supplemental_true_check_allowed():
    gate = _pass_protocol_gate(_protocol_checks(extra_supplemental=True))
    assert gate.status is GateStatus.PASS


def test_full_required_set_passes():
    gate = _pass_protocol_gate(_protocol_checks())
    assert require_gate_pass(gate).ok


def test_pass_requires_provenance():
    with pytest.raises(ValidationError):
        GateArtifact(
            gate_name="PROTOCOL-READY",
            status=GateStatus.PASS,
            engine_git_sha="abc",
            mandatory_checks=_protocol_checks(),
            evidence=_EV,
            created_at=TS,  # no provenance
        )


def test_hold_gate_integrity_ok_but_not_promotable():
    hold = GateArtifact(
        gate_name="PROTOCOL-READY",
        status=GateStatus.HOLD,
        engine_git_sha="abc",
        mandatory_checks={"all_tests_pass": False},
        created_at=TS,
    )
    assert verify_gate_integrity(hold).ok  # structurally sound
    promotion = require_gate_pass(hold)
    assert not promotion.ok
    assert any("not PASS" in r for r in promotion.reasons)
