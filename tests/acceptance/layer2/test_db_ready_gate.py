"""DB-READY gate: required checks, assembly status logic, and tamper verification."""

from __future__ import annotations

import json

import pytest

from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.layer2_db_runner import (
    _CHECK_NODES,
    ALEMBIC_HEAD,
    GATE_NAME,
    MIGRATION_FILE,
    alembic_head,
    assemble_db_result,
    verify_db_ready_gate,
)
from minos_engine.qualification.provenance import read_provenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import SourceIntegrity
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "db-ready.json"
_H = "a" * 64


def _passmap(all_pass: bool = True) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for nodes in _CHECK_NODES.values():
        for n in nodes:
            key = n if "::" in n else f"{n}::case1"
            out[key] = all_pass
    return out


def _synthetic_source_integrity() -> SourceIntegrity:
    return SourceIntegrity(
        evidence=(
            EvidenceItem(
                description="migration", path=MIGRATION_FILE, kind=EvidenceKind.FILE, sha256=_H
            ),
        ),
        evidence_hashes_complete=True,
        required_source_tracked=True,
        worktree_matches_head=True,
        spec_manifest_hash=_H,
    )


def _assemble(passmap: dict[str, bool]):
    return assemble_db_result(
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
            line_coverage_percent=95.0, covered_lines=95, valid_lines=100, missing_lines=5, tool="c"
        ),
        tools={"ruff": True, "format": True, "mypy": True},
        provenance=read_provenance(REPO_ROOT),
        source_integrity=_synthetic_source_integrity(),
        created_at="2026-08-17T00:00:00+00:00",
    )


# --------------------------------------------------------------------------- #
# Static structure
# --------------------------------------------------------------------------- #
def test_db_ready_required_checks_registered():
    required = required_checks_for("DB-READY")
    assert required, "DB-READY must have a registered required-check set"
    for check in (
        "l2a_entry_passed",
        "seven_schemas_created",
        "five_roles_created",
        "append_only_passed",
        "least_privilege_passed",
        "live_evaluation_denied",
        "worker_claim_concurrency_passed",
        "service_still_blocked",
        "split_manifest_absent",
        "accepted_prerequisites_unchanged",
    ):
        assert check in required


def test_alembic_head_matches_constant():
    assert alembic_head() == ALEMBIC_HEAD


# --------------------------------------------------------------------------- #
# Assembly status logic (no PostgreSQL required)
# --------------------------------------------------------------------------- #
def test_assemble_pass_when_all_checks_true():
    result = _assemble(_passmap(all_pass=True))
    assert result.gate.status is GateStatus.PASS
    assert required_checks_for(GATE_NAME) <= set(result.gate.mandatory_checks)
    # the gate binds the storage/role/alembic identities
    ih = result.gate.input_hashes
    assert ih["alembic_head_revision"] == ALEMBIC_HEAD
    assert ih["postgres_major_version"] == "16"
    assert ih["accepted_l1_ready_gate_hash"].startswith("aeabfea8")


def test_assemble_holds_when_a_behavior_check_fails():
    pm = _passmap(all_pass=True)
    pm["test_worker_claim.py::case1"] = False
    result = _assemble(pm)
    assert result.gate.status is GateStatus.HOLD
    assert result.gate.mandatory_checks["worker_claim_concurrency_passed"] is False


# --------------------------------------------------------------------------- #
# Committed-gate verification + tamper (skips until Commit P adds the gate)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit P")
def test_committed_db_gate_verifies():
    result = verify_db_ready_gate(REPO_ROOT, _GATE, require_descends=True)
    assert result.ok, result.reasons


def _tamper(tmp_path, mutate) -> str:
    raw = json.loads(_GATE.read_text(encoding="utf-8"))
    mutate(raw)
    raw["gate_hash"] = ""
    gate = GateArtifact.model_validate(raw)
    path = tmp_path / "gates" / "db-ready.json"
    write_gate(gate, path)
    return str(path)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit P")
def test_tampered_storage_hash_rejected(tmp_path):
    def mut(raw):
        raw["input_hashes"] = dict(raw["input_hashes"], storage_schema_hash="9" * 64)

    from pathlib import Path

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("storage_schema_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit P")
def test_tampered_accepted_prereq_rejected(tmp_path):
    def mut(raw):
        raw["input_hashes"] = dict(raw["input_hashes"], accepted_l1_ready_gate_hash="9" * 64)

    from pathlib import Path

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("accepted_l1_unchanged" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit P")
def test_wrong_qualification_tool_rejected(tmp_path):
    def mut(raw):
        raw["qualification_tool_version"] = "bogus"

    from pathlib import Path

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("qualification_tool_identity" in r for r in result.reasons)
