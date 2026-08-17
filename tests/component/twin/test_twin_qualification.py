"""Test group I — TWIN-READY qualification assembly and gate enforcement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import require_gate_pass, verify_gate_integrity
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.provenance import GitProvenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import _EVIDENCE, SourceIntegrity
from minos_engine.qualification.twin_runner import assemble_twin_result
from tests.conftest import REPO_ROOT

_TS = "2026-08-17T12:00:00+00:00"
_ACC = PytestAccounting(
    collected=300, passed=300, failed=0, errors=0, skipped=0, duration_seconds=1.0, exit_code=0
)
_COV = CoverageResult(
    line_coverage_percent=95.0,
    covered_lines=950,
    valid_lines=1000,
    missing_lines=50,
    tool="coverage.py",
)
_PROV = GitProvenance(
    head_sha="a" * 40, tree_sha="b" * 40, worktree_clean=True, parent_sha="c" * 40
)
_TOOLS = {"ruff_check": True, "ruff_format": True, "mypy": True}


def _si(**over) -> SourceIntegrity:
    evidence = tuple(
        EvidenceItem(description=rel, path=rel, kind=kind, sha256="a" * 64)
        for rel, kind in _EVIDENCE
    )
    kw = {
        "evidence": evidence,
        "evidence_hashes_complete": True,
        "required_source_tracked": True,
        "worktree_matches_head": True,
        "spec_manifest_hash": "b" * 64,
    }
    kw.update(over)
    return SourceIntegrity(**kw)


def _assemble(**over):
    kw = {
        "accounting": _ACC,
        "coverage": _COV,
        "tools": dict(_TOOLS),
        "provenance": _PROV,
        "source_integrity": _si(),
        "twin_required_tracked": True,
        "created_at": _TS,
    }
    kw.update(over)
    return assemble_twin_result(REPO_ROOT, **kw)


def test_twin_qualification_pass_on_clean_repo():
    result = _assemble()
    gate = result.gate
    assert gate.status is GateStatus.PASS, [k for k, v in gate.mandatory_checks.items() if not v]
    assert gate.gate_name == "TWIN-READY"
    assert result.declared_parity_level.value == "FIXTURE_REPLAY"
    assert gate.input_hashes["declared_parity_level"] == "FIXTURE_REPLAY"
    assert require_gate_pass(gate).ok
    assert verify_gate_integrity(gate).ok


def test_reject_when_tool_fails():
    result = _assemble(tools={"ruff_check": True, "ruff_format": True, "mypy": False})
    assert result.gate.status is GateStatus.REJECT
    assert result.gate.mandatory_checks["mypy_pass"] is False


def test_reject_when_twin_source_untracked():
    result = _assemble(twin_required_tracked=False)
    assert result.gate.status is GateStatus.REJECT
    assert result.gate.mandatory_checks["required_source_tracked"] is False


def test_reject_when_tests_fail():
    bad = PytestAccounting(
        collected=300, passed=290, failed=10, errors=0, skipped=0, duration_seconds=1.0, exit_code=1
    )
    result = _assemble(accounting=bad)
    assert result.gate.status is GateStatus.REJECT


def _pass_checks(**over):
    checks = dict.fromkeys(required_checks_for("TWIN-READY"), True)
    checks.update(over)
    return checks


def test_twin_gate_required_checks_enforced():
    ev = (EvidenceItem(description="e", path="reports/x.md", sha256="a" * 64),)
    # Missing a required check cannot construct a PASS gate.
    checks = _pass_checks()
    checks.pop("truth_isolation_ok")
    with pytest.raises(ValidationError):
        GateArtifact(
            gate_name="TWIN-READY",
            status=GateStatus.PASS,
            engine_git_sha="x",
            mandatory_checks=checks,
            evidence=ev,
            qualified_source_git_sha="s",
            qualified_source_tree_sha="t",
            qualification_tool_version="v",
            created_at=_TS,
        )


def test_twin_gate_arbitrary_dict_cannot_pass():
    with pytest.raises(ValidationError):
        GateArtifact(
            gate_name="TWIN-READY",
            status=GateStatus.PASS,
            engine_git_sha="x",
            mandatory_checks={"anything": True},
            evidence=(EvidenceItem(description="e", path="reports/x.md", sha256="a" * 64),),
            qualified_source_git_sha="s",
            qualified_source_tree_sha="t",
            qualification_tool_version="v",
            created_at=_TS,
        )
