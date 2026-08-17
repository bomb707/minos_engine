"""L1-READY qualification assembly + gate enforcement (synthetic git inputs)."""

from __future__ import annotations

from tests.conftest import REPO_ROOT

from minos_engine.gates.contracts import EvidenceItem, GateStatus
from minos_engine.qualification import layer1_runner as R
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.provenance import GitProvenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import SourceIntegrity

_TS = "2026-08-17T12:00:00+00:00"
_ACC = PytestAccounting(
    collected=400, passed=400, failed=0, errors=0, skipped=0, duration_seconds=1.0, exit_code=0
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
        for rel, kind in R.LAYER1_EVIDENCE
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
        "created_at": _TS,
    }
    kw.update(over)
    return R.assemble_layer1_result(REPO_ROOT, **kw)


def test_l1_ready_pass_on_clean_repo():
    result = _assemble()
    gate = result.gate
    failing = [k for k, v in gate.mandatory_checks.items() if not v]
    assert gate.status is GateStatus.PASS, failing
    assert gate.gate_name == "L1-READY"
    assert gate.qualification_tool_version == R.LAYER1_QUALIFIER_VERSION
    assert gate.mandatory_checks["layer1_real_bam_qualified"] is True
    assert gate.mandatory_checks["layer1_hard_limit_met"] is True
    assert gate.mandatory_checks["twin_ready_identity_accepted"] is True
    assert gate.mandatory_checks["protocol_ready_identity_accepted"] is True
    assert gate.input_hashes["profiler_version"] == "layer1-profiler-v1"
    assert gate.input_hashes["prerequisite_twin_ready_gate_hash"].startswith("3464fb76")
    assert result.real_bam_qualified is True


def test_hold_when_tool_fails():
    result = _assemble(tools={"ruff_check": True, "ruff_format": True, "mypy": False})
    assert result.gate.status is GateStatus.HOLD
    assert result.gate.mandatory_checks["mypy_pass"] is False


def test_hold_when_real_bam_unavailable(monkeypatch):
    monkeypatch.setattr(R, "load_integration_report", lambda root: None)
    result = _assemble()
    assert result.gate.status is GateStatus.HOLD
    assert result.gate.mandatory_checks["layer1_real_bam_qualified"] is False
    assert result.gate.mandatory_checks["layer1_hard_limit_met"] is False


def test_hold_when_source_untracked():
    result = _assemble(source_integrity=_si(required_source_tracked=False))
    assert result.gate.status is GateStatus.HOLD
    assert result.gate.mandatory_checks["required_source_tracked"] is False


def test_required_check_set_is_complete():
    from minos_engine.gates.required_checks import required_checks_for

    result = _assemble()
    required = required_checks_for("L1-READY")
    assert required.issubset(set(result.gate.mandatory_checks))
    assert len(required) == 34
