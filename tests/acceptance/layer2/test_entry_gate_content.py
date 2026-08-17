"""Evidence-hygiene and mandatory-check negatives for the L2 entry gate.

Each negative writes a crafted gate at the canonical path in a throwaway
``repo_root`` and asserts the targeted content reason surfaces (the throwaway root
has no git history, an accepted extra reason).
"""

from __future__ import annotations

import json

import pytest

from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.layer2.entry_gate import EntryGateRequest, verify_l2_entry_gate
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "l1-ready.json"
pytestmark = pytest.mark.skipif(not _GATE.exists(), reason="L1-READY gate produced in Commit B")
_H64 = "a" * 64
TS = "2026-08-17T00:00:00+00:00"


def _write_repo(tmp_path, gate: GateArtifact) -> EntryGateRequest:
    write_gate(gate, tmp_path / "gates" / "l1-ready.json")
    rp = tmp_path / "reports" / "LAYER1_QUALIFICATION_REPORT.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("r", encoding="utf-8")
    return EntryGateRequest(repo_root=str(tmp_path))


def _hold(**over) -> GateArtifact:
    kwargs = {
        "gate_name": "L1-READY",
        "status": GateStatus.HOLD,
        "engine_git_sha": "x",
        "mandatory_checks": {"placeholder": True},
        "evidence": (EvidenceItem(description="e", path="reports/x.md", sha256=_H64),),
        "created_at": TS,
    }
    kwargs.update(over)
    return GateArtifact(**kwargs)


def test_status_not_pass_rejected(tmp_path):
    result = verify_l2_entry_gate(_write_repo(tmp_path, _hold()))
    assert not result.ok
    assert "GATE_STATUS_NOT_PASS" in result.reasons


def test_missing_required_checks_rejected(tmp_path):
    result = verify_l2_entry_gate(_write_repo(tmp_path, _hold(mandatory_checks={"only": True})))
    assert not result.ok
    assert "REQUIRED_CHECKS_MISSING" in result.reasons


def test_false_mandatory_check_rejected(tmp_path):
    checks = dict.fromkeys(required_checks_for("L1-READY"), True)
    checks["something"] = False
    result = verify_l2_entry_gate(_write_repo(tmp_path, _hold(mandatory_checks=checks)))
    assert not result.ok
    assert "MANDATORY_CHECK_FALSE" in result.reasons


def test_empty_evidence_rejected(tmp_path):
    result = verify_l2_entry_gate(_write_repo(tmp_path, _hold(evidence=())))
    assert not result.ok
    assert "EVIDENCE_EMPTY" in result.reasons


def test_duplicate_evidence_path_rejected(tmp_path):
    ev = (
        EvidenceItem(description="a", path="reports/x.md", sha256=_H64),
        EvidenceItem(description="b", path="reports/x.md", sha256=_H64),
    )
    result = verify_l2_entry_gate(_write_repo(tmp_path, _hold(evidence=ev)))
    assert not result.ok
    assert "EVIDENCE_DUPLICATE_PATH" in result.reasons


def test_absolute_evidence_path_rejected(tmp_path):
    ev = (EvidenceItem(description="a", path="/etc/passwd", sha256=_H64),)
    result = verify_l2_entry_gate(_write_repo(tmp_path, _hold(evidence=ev)))
    assert not result.ok
    assert "EVIDENCE_PATH_ESCAPE" in result.reasons


def test_parent_escape_evidence_path_rejected(tmp_path):
    ev = (EvidenceItem(description="a", path="../escape", sha256=_H64),)
    result = verify_l2_entry_gate(_write_repo(tmp_path, _hold(evidence=ev)))
    assert not result.ok
    assert "EVIDENCE_PATH_ESCAPE" in result.reasons


def test_symlink_escape_evidence_path_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    ev = (EvidenceItem(description="a", path="link/secret", sha256=_H64),)
    result = verify_l2_entry_gate(_write_repo(root, _hold(evidence=ev)))
    assert not result.ok
    assert "EVIDENCE_SYMLINK_ESCAPE" in result.reasons


def test_malformed_sha_rejected(tmp_path):
    raw = json.loads(_GATE.read_text(encoding="utf-8"))
    raw["evidence"][0]["sha256"] = "not-a-valid-sha"
    path = tmp_path / "gates" / "l1-ready.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    result = verify_l2_entry_gate(EntryGateRequest(repo_root=str(tmp_path)))
    assert not result.ok
    assert any(r in result.reasons for r in ("GATE_SCHEMA_INVALID", "GATE_MALFORMED"))
