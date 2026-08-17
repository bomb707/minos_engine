"""Prerequisite identity + evidence verification, and TWIN-READY check mode."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.qualification.twin_runner import verify_twin_ready
from minos_engine.twin.prerequisites import (
    ACCEPTED_GATE_HASH,
    verify_protocol_ready,
)
from tests.conftest import REPO_ROOT

_TS = "2026-08-17T12:00:00+00:00"


def test_accepted_prerequisite_fully_verifies_on_real_repo():
    result = verify_protocol_ready(REPO_ROOT)
    assert result.identity_accepted
    assert result.evidence_verified
    assert result.promotion_authorized
    assert result.ok
    assert result.gate_hash == ACCEPTED_GATE_HASH


def test_missing_prerequisite_gate_fails(tmp_path):
    result = verify_protocol_ready(REPO_ROOT, gate_path=tmp_path / "nope.json")
    assert not result.identity_accepted
    assert not result.ok


def test_modified_prerequisite_gate_fails(tmp_path):
    raw = json.loads((REPO_ROOT / "gates" / "protocol-ready.json").read_text())
    raw["mandatory_checks"]["all_tests_pass"] = False  # tamper (hash no longer matches)
    p = tmp_path / "protocol-ready.json"
    p.write_text(json.dumps(raw))
    result = verify_protocol_ready(REPO_ROOT, gate_path=p)
    assert not result.identity_accepted
    assert not result.ok


def _replacement_protocol_ready() -> GateArtifact:
    checks = dict.fromkeys(required_checks_for("PROTOCOL-READY"), True)
    return GateArtifact(
        gate_name="PROTOCOL-READY",
        status=GateStatus.PASS,
        engine_git_sha="replacement",
        mandatory_checks=checks,
        evidence=(EvidenceItem(description="e", path="reports/x.md", sha256="a" * 64),),
        qualified_source_git_sha="0" * 40,
        qualified_source_tree_sha="1" * 40,
        qualification_tool_version="v",
        created_at=_TS,
    )


def test_unaccepted_replacement_gate_fails(tmp_path):
    # A structurally valid PASS PROTOCOL-READY gate that is NOT the accepted one.
    gate = _replacement_protocol_ready()
    p = tmp_path / "protocol-ready.json"
    p.write_text(json.dumps(gate.model_dump(mode="json")))
    result = verify_protocol_ready(REPO_ROOT, gate_path=p)
    assert not result.identity_accepted  # hash / source / tree are not accepted
    assert not result.ok


def _temp_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    return root


def test_missing_qualified_commit_fails(tmp_path):
    # Copy the accepted gate into a fresh repo that does NOT contain commit 4a5a14d.
    root = _temp_repo(tmp_path)
    (root / "gates").mkdir()
    (root / "gates" / "protocol-ready.json").write_text(
        (REPO_ROOT / "gates" / "protocol-ready.json").read_text()
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"], check=True)
    result = verify_protocol_ready(root)
    # Identity has the accepted hash/source/tree, but the commit is absent locally.
    assert not result.identity_accepted
    assert any("commit" in r.lower() for r in result.reasons)


def test_verify_twin_ready_missing_gate(tmp_path):
    result = verify_twin_ready(REPO_ROOT, tmp_path / "nope.json", require_descends=False)
    assert not result.ok


def test_verify_twin_ready_rejects_wrong_gate_name(tmp_path):
    # A PROTOCOL-READY gate is not a TWIN-READY gate.
    gate = _replacement_protocol_ready()
    p = tmp_path / "twin-ready.json"
    p.write_text(json.dumps(gate.model_dump(mode="json")))
    result = verify_twin_ready(REPO_ROOT, p, require_descends=False)
    assert not result.ok
    assert result.checks.get("gate_name_twin_ready") is False
