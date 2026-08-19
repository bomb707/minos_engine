"""Cover the INGEST-READY assemble path with synthetic inputs (no subprocess/PG)."""

from __future__ import annotations

from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.layer2_ingest_runner import (
    _CHECK_NODES,
    ATTESTATION_SCHEMA_FILE,
    GATE_NAME,
    INGEST_PACKAGE_DIR,
    L2D_MIGRATION_FILE,
    assemble_ingest_result,
)
from minos_engine.qualification.provenance import read_provenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import SourceIntegrity
from tests.conftest import REPO_ROOT

_H = "a" * 64


def _si() -> SourceIntegrity:
    ev = (
        EvidenceItem(description="m", path=L2D_MIGRATION_FILE, kind=EvidenceKind.FILE, sha256=_H),
        EvidenceItem(
            description="s", path=ATTESTATION_SCHEMA_FILE, kind=EvidenceKind.FILE, sha256=_H
        ),
        EvidenceItem(
            description="p", path=INGEST_PACKAGE_DIR, kind=EvidenceKind.DIRECTORY, sha256=_H
        ),
    )
    return SourceIntegrity(
        evidence=ev,
        evidence_hashes_complete=True,
        required_source_tracked=True,
        worktree_matches_head=True,
        spec_manifest_hash=_H,
    )


def test_assemble_produces_pass_gate_with_all_required_checks() -> None:
    passmap: dict[str, bool] = {}
    for nodes in _CHECK_NODES.values():
        for n in nodes:
            passmap[n if "::" in n else f"{n}::case1"] = True
    result = assemble_ingest_result(
        REPO_ROOT,
        passmap=passmap,
        accounting=PytestAccounting(
            collected=10,
            passed=10,
            failed=0,
            errors=0,
            skipped=0,
            duration_seconds=1.0,
            exit_code=0,
        ),
        coverage=CoverageResult(
            line_coverage_percent=95.0,
            covered_lines=95,
            valid_lines=100,
            missing_lines=5,
            tool="c",
        ),
        tools={"ruff": True, "format": True, "mypy": True},
        provenance=read_provenance(REPO_ROOT),
        source_integrity=_si(),
        created_at="2026-08-19T00:00:00+00:00",
    )
    gate = result.gate
    assert gate.status is GateStatus.PASS, [k for k, v in gate.mandatory_checks.items() if not v]
    required = required_checks_for(GATE_NAME)
    assert required <= set(gate.mandatory_checks)
    assert all(gate.mandatory_checks[c] for c in required)
    assert gate.input_hashes["accepted_split_frozen_v2_gate_hash"].startswith("6bd9f472")
