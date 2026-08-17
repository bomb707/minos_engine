"""Test group I — TWIN-READY qualification assembly, gate enforcement, identity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.provenance import GitProvenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import SourceIntegrity
from minos_engine.qualification.twin_runner import (
    STAGE1_EVIDENCE,
    STAGE1_TWIN_QUALIFIER_VERSION,
    assemble_twin_result,
)
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
        for rel, kind in STAGE1_EVIDENCE
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
    return assemble_twin_result(REPO_ROOT, **kw)


def test_twin_qualification_machinery_intact_after_stage_advance():
    # We have advanced to the Layer 1 stage: rebuilding TWIN-READY at the live
    # tree now REJECTs solely because `layer1_not_implemented` flipped to False.
    # Every other required check still passes. The accepted committed TWIN-READY
    # gate is unaffected (re-verified by re-hashing its committed source).
    result = _assemble()
    gate = result.gate
    failing = [k for k, v in gate.mandatory_checks.items() if not v]
    assert failing == ["layer1_not_implemented"], failing
    assert gate.status is GateStatus.REJECT
    assert gate.gate_name == "TWIN-READY"
    assert gate.qualification_tool_version == STAGE1_TWIN_QUALIFIER_VERSION
    assert gate.input_hashes["declared_parity_level"] == "FIXTURE_REPLAY"
    assert gate.input_hashes["prerequisite_protocol_ready_gate_hash"].startswith("b9cda0ba")
    assert gate.mandatory_checks["protocol_ready_identity_accepted"] is True
    assert gate.mandatory_checks["protocol_ready_evidence_verified"] is True


def test_accepted_twin_ready_still_verifies():
    from minos_engine.qualification.twin_runner import verify_twin_ready

    v = verify_twin_ready(
        REPO_ROOT, REPO_ROOT / "gates" / "twin-ready.json", require_descends=False
    )
    assert v.ok, v.reasons


def test_gate_records_python_runtime():
    result = _assemble()
    assert result.gate.input_hashes["python_runtime"] == "CPython 3.12"
    assert result.gate.mandatory_checks["python_runtime_is_3_12"] is True


def test_twin_ready_cannot_pass_if_runtime_false():
    checks = _pass_checks(python_runtime_is_3_12=False)
    with pytest.raises(ValidationError):
        GateArtifact(
            gate_name="TWIN-READY",
            status=GateStatus.PASS,
            engine_git_sha="x",
            mandatory_checks=checks,
            evidence=(EvidenceItem(description="e", path="reports/x.md", sha256="a" * 64),),
            qualified_source_git_sha="s",
            qualified_source_tree_sha="t",
            qualification_tool_version="v",
            created_at=_TS,
        )


def test_stage1_qualifier_identity_changes_gate_hash():
    result = _assemble()
    base = result.gate.model_dump(mode="json")
    base.pop("gate_hash")
    base["qualification_tool_version"] = "stage0-qualifier-v2"
    altered = GateArtifact.model_validate(base)
    assert altered.gate_hash != result.gate.gate_hash


def test_reject_when_tool_fails():
    result = _assemble(tools={"ruff_check": True, "ruff_format": True, "mypy": False})
    assert result.gate.status is GateStatus.REJECT
    assert result.gate.mandatory_checks["mypy_pass"] is False


def test_reject_when_required_source_untracked():
    result = _assemble(source_integrity=_si(required_source_tracked=False))
    assert result.gate.status is GateStatus.REJECT
    assert result.gate.mandatory_checks["required_source_tracked"] is False


def test_reject_when_worktree_drifts():
    result = _assemble(source_integrity=_si(worktree_matches_head=False))
    assert result.gate.status is GateStatus.REJECT


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
    checks = _pass_checks()
    checks.pop("protocol_ready_identity_accepted")
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
