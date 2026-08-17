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
        evidence=(EvidenceItem(description="qualification", path="reports/q.md", sha256="a" * 64),),
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


def test_evidence_tamper_fails_integrity(tmp_path):
    from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
    from minos_engine.gates.verifier import verify_gate_integrity
    from minos_engine.qualification.evidence import sha256_file

    evidence_file = tmp_path / "reports" / "audit.md"
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    evidence_file.write_text("original evidence", encoding="utf-8")
    gate = GateArtifact(
        gate_name="TEST",
        status=GateStatus.PASS,
        engine_git_sha="abc",
        mandatory_checks={"a": True},
        evidence=(
            EvidenceItem(
                description="audit",
                path="reports/audit.md",
                sha256=sha256_file(evidence_file),
            ),
        ),
        created_at="2026-08-17T12:00:00+00:00",
    )
    assert verify_gate_integrity(gate, base_dir=tmp_path).ok
    # Tamper the evidence file -> integrity must fail.
    evidence_file.write_text("TAMPERED", encoding="utf-8")
    result = verify_gate_integrity(gate, base_dir=tmp_path)
    assert not result.ok
    assert any("EVIDENCE_HASH_MISMATCH" in r for r in result.reasons)
