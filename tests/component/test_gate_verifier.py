"""Gate verifier: write, verify, tamper detection, and CLI gate-verify path."""

from __future__ import annotations

import json

from minos_engine.cli.main import main
from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.verifier import verify_gate_file, write_gate


def _pass_gate() -> GateArtifact:
    return GateArtifact(
        gate_name="TEST-READY",
        status=GateStatus.PASS,
        engine_git_sha="abc123",
        mandatory_checks={"determinism": True, "gatk_only": True},
        evidence=(EvidenceItem(description="qualification", path="reports/q.md"),),
        created_at="2026-08-17T12:00:00+00:00",
    )


def test_write_then_verify_ok(tmp_path):
    p = tmp_path / "gates" / "test-ready.json"
    write_gate(_pass_gate(), p)
    result = verify_gate_file(p)
    assert result.ok
    assert result.status is GateStatus.PASS


def test_verify_detects_tampered_hash(tmp_path):
    p = tmp_path / "gate.json"
    write_gate(_pass_gate(), p)
    raw = json.loads(p.read_text())
    raw["gate_hash"] = "0" * 64
    p.write_text(json.dumps(raw))
    assert not verify_gate_file(p).ok


def test_cli_gate_verify_pass(tmp_path, capsys):
    p = tmp_path / "gate.json"
    write_gate(_pass_gate(), p)
    assert main(["gate", "verify", "--gate", str(p), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["status"] == "PASS"
