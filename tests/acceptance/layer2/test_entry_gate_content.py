"""Evidence-hygiene and mandatory-check negatives for the L2 entry gate.

Gate-content negatives are isolated by pointing the locator at a tampered gate
while the real repository supplies a passing git-ancestry chain (so only the
targeted content reason must be asserted). Symlink/non-git cases use a throwaway
directory and tolerate the additional ``NOT_A_GIT_REPO`` reason.
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


def _write(tmp_path, gate: GateArtifact) -> str:
    path = tmp_path / "gates" / "l1-ready.json"
    write_gate(gate, path)
    return str(path)


def _req(tmp_gate: str, root=REPO_ROOT) -> EntryGateRequest:
    return EntryGateRequest(repo_root=str(root), l1_ready_path=tmp_gate)


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
    result = verify_l2_entry_gate(_req(_write(tmp_path, _hold())))
    assert not result.ok
    assert "GATE_STATUS_NOT_PASS" in result.reasons


def test_missing_required_checks_rejected(tmp_path):
    result = verify_l2_entry_gate(_req(_write(tmp_path, _hold(mandatory_checks={"only": True}))))
    assert not result.ok
    assert "REQUIRED_CHECKS_MISSING" in result.reasons


def test_false_mandatory_check_rejected(tmp_path):
    checks = dict.fromkeys(required_checks_for("L1-READY"), True)
    checks["something"] = False
    result = verify_l2_entry_gate(_req(_write(tmp_path, _hold(mandatory_checks=checks))))
    assert not result.ok
    assert "MANDATORY_CHECK_FALSE" in result.reasons


def test_empty_evidence_rejected(tmp_path):
    result = verify_l2_entry_gate(_req(_write(tmp_path, _hold(evidence=()))))
    assert not result.ok
    assert "EVIDENCE_EMPTY" in result.reasons


def test_duplicate_evidence_path_rejected(tmp_path):
    ev = (
        EvidenceItem(description="a", path="reports/x.md", sha256=_H64),
        EvidenceItem(description="b", path="reports/x.md", sha256=_H64),
    )
    result = verify_l2_entry_gate(_req(_write(tmp_path, _hold(evidence=ev))))
    assert not result.ok
    assert "EVIDENCE_DUPLICATE_PATH" in result.reasons


def test_absolute_evidence_path_rejected(tmp_path):
    ev = (EvidenceItem(description="a", path="/etc/passwd", sha256=_H64),)
    result = verify_l2_entry_gate(_req(_write(tmp_path, _hold(evidence=ev))))
    assert not result.ok
    assert "EVIDENCE_PATH_ESCAPE" in result.reasons


def test_parent_escape_evidence_path_rejected(tmp_path):
    ev = (EvidenceItem(description="a", path="../escape", sha256=_H64),)
    result = verify_l2_entry_gate(_req(_write(tmp_path, _hold(evidence=ev))))
    assert not result.ok
    assert "EVIDENCE_PATH_ESCAPE" in result.reasons


def test_symlink_escape_evidence_path_rejected(tmp_path):
    # A throwaway (non-git) root with a symlink escaping outside it.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    ev = (EvidenceItem(description="a", path="link/secret", sha256=_H64),)
    gate_path = _write(root, _hold(evidence=ev))
    result = verify_l2_entry_gate(EntryGateRequest(repo_root=str(root), l1_ready_path=gate_path))
    assert not result.ok
    assert "EVIDENCE_SYMLINK_ESCAPE" in result.reasons


def test_malformed_sha_rejected(tmp_path):
    # Raw JSON whose evidence sha256 is not 64 hex — schema/model rejects it.
    raw = json.loads(_GATE.read_text(encoding="utf-8"))
    raw["evidence"][0]["sha256"] = "not-a-valid-sha"
    path = tmp_path / "gates" / "l1-ready.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    result = verify_l2_entry_gate(_req(str(path)))
    assert not result.ok
    assert any(r in result.reasons for r in ("GATE_SCHEMA_INVALID", "GATE_MALFORMED"))
