"""Qualification runner assembly, checks, provenance (no subprocess recursion)."""

from __future__ import annotations

import json

from minos_engine.gates.contracts import GateStatus
from minos_engine.qualification import checks as C
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.fixtures import raw_response, valid_raw_payload
from minos_engine.qualification.provenance import GitProvenance, read_provenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import assemble_result, write_outputs
from tests.conftest import REPO_ROOT

_ACC_OK = PytestAccounting(
    collected=189, passed=189, failed=0, errors=0, skipped=0, duration_seconds=1.0, exit_code=0
)
_COV_OK = CoverageResult(
    line_coverage_percent=95.0,
    covered_lines=950,
    valid_lines=1000,
    missing_lines=50,
    tool="coverage.py",
)
_PROV_OK = GitProvenance(
    head_sha="a" * 40, tree_sha="b" * 40, worktree_clean=True, parent_sha="c" * 40
)
_TOOLS_OK = {"ruff_check": True, "ruff_format": True, "mypy": True}
_TS = "2026-08-17T12:00:00+00:00"


def _assemble(**overrides):
    kwargs = {
        "accounting": _ACC_OK,
        "coverage": _COV_OK,
        "tools": dict(_TOOLS_OK),
        "provenance": _PROV_OK,
        "created_at": _TS,
    }
    kwargs.update(overrides)
    return assemble_result(REPO_ROOT, **kwargs)


def test_assemble_pass_gate_on_clean_repo():
    result = _assemble()
    gate = result.gate
    assert gate.status is GateStatus.PASS, [k for k, v in gate.mandatory_checks.items() if not v]
    assert gate.qualified_source_git_sha == "a" * 40
    assert gate.qualification_tool_version
    assert all(e.sha256 for e in gate.evidence)
    assert "PROTOCOL-READY" in result.report_markdown


def test_assemble_reject_when_a_tool_fails():
    result = _assemble(tools={"ruff_check": True, "ruff_format": True, "mypy": False})
    assert result.gate.status is GateStatus.REJECT
    assert result.gate.mandatory_checks["mypy_pass"] is False


def test_assemble_reject_when_tests_fail():
    bad = PytestAccounting(
        collected=189, passed=180, failed=9, errors=0, skipped=0, duration_seconds=1.0, exit_code=1
    )
    result = _assemble(accounting=bad)
    assert result.gate.status is GateStatus.REJECT
    assert result.gate.mandatory_checks["all_tests_pass"] is False


def test_assemble_reject_when_coverage_below_threshold():
    low = CoverageResult(
        line_coverage_percent=80.0,
        covered_lines=800,
        valid_lines=1000,
        missing_lines=200,
        tool="coverage.py",
    )
    result = _assemble(coverage=low)
    assert result.gate.status is GateStatus.REJECT
    assert result.gate.mandatory_checks["coverage_threshold_met"] is False


def test_write_outputs(tmp_path):
    result = _assemble()
    gate_path, report_path = write_outputs(result, tmp_path)
    assert gate_path.exists()
    assert report_path.exists()
    written = json.loads(gate_path.read_text())
    assert written["gate_name"] == "PROTOCOL-READY"


def test_checks_on_real_repo():
    assert C.gatk_registry_has_25_params()
    assert C.canonical_determinism()
    assert C.gatk_only_policy()
    assert C.required_identity_fails_closed()
    assert C.layer1_not_implemented()
    assert C.layer2_blocked()
    assert C.six_schemas_present(REPO_ROOT)
    assert C.documentation_complete(REPO_ROOT)
    assert C.ci_workflow_present(REPO_ROOT)
    assert C.specifications_present(REPO_ROOT)
    assert C.no_truth_or_locked_test_access(REPO_ROOT / "src" / "minos_engine")


def test_fixtures_roundtrip():
    resp = raw_response()
    assert resp.payload["round"]["region"].startswith("chr19")
    assert valid_raw_payload()["provenance"]["scorer_hash"]


def test_provenance_reads_repo():
    prov = read_provenance(REPO_ROOT)
    # This repo is a git repo; head/tree should resolve to 40-hex shas.
    assert prov.head_sha and len(prov.head_sha) == 40
    assert prov.tree_sha and len(prov.tree_sha) == 40
