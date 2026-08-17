"""Comprehensive L1-READY entry-gate verification (item 8).

Builds a fully-valid L1-READY gate + qualification report, confirms it verifies,
then mutates each field to prove every rejection path. Layer 2 stays blocked
because no legitimate L1-READY exists in the repo.
"""

from __future__ import annotations

import json

import pytest

from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.layer2.entry_gate import EntryGateRequest, verify_l1_ready
from minos_engine.qualification.evidence import sha256_file

SCHEMA_H = "1" * 64
CFG_H = "2" * 64
PROFILER_VERSION = "l1-profiler-v1"
SRC_SHA = "3" * 64
TREE_SHA = "4" * 64
TS = "2026-08-17T12:00:00+00:00"


def _l1_required_true() -> dict[str, bool]:
    return dict.fromkeys(required_checks_for("L1-READY"), True)


def _write_report(tmp_path) -> tuple[str, str]:
    report = tmp_path / "reports" / "l1-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"stage": "S3", "result": "L1-READY"}), encoding="utf-8")
    return str(report), sha256_file(report)


def _build_gate(tmp_path, *, report_hash: str, evidence_path: str, evidence_sha: str) -> str:
    gate = GateArtifact(
        gate_name="L1-READY",
        status=GateStatus.PASS,
        engine_git_sha="abc123",
        input_hashes={
            "layer1_schema_hash": SCHEMA_H,
            "profiler_config_hash": CFG_H,
            "profiler_version": PROFILER_VERSION,
            "qualification_report_hash": report_hash,
        },
        evidence=(EvidenceItem(description="report", path=evidence_path, sha256=evidence_sha),),
        mandatory_checks=_l1_required_true(),
        qualified_source_git_sha=SRC_SHA,
        qualified_source_tree_sha=TREE_SHA,
        qualification_tool_version="stage3-qualifier-v1",
        created_at=TS,
    )
    path = tmp_path / "gates" / "l1-ready.json"
    write_gate(gate, path)
    return str(path)


def _request(tmp_path, gate_path: str, report_path: str, **overrides) -> EntryGateRequest:
    kwargs = {
        "l1_ready_path": gate_path,
        "qualification_report_path": report_path,
        "expected_layer1_schema_hash": SCHEMA_H,
        "expected_profiler_config_hash": CFG_H,
        "expected_profiler_version": PROFILER_VERSION,
        "base_dir": str(tmp_path),
        "expected_qualified_source_git_sha": SRC_SHA,
        "expected_qualified_source_tree_sha": TREE_SHA,
    }
    kwargs.update(overrides)
    return EntryGateRequest(**kwargs)


def _valid(tmp_path):
    report_path, report_hash = _write_report(tmp_path)
    # Evidence points at the report file (relative to base_dir) so re-hash matches.
    rel = "reports/l1-report.json"
    gate_path = _build_gate(
        tmp_path, report_hash=report_hash, evidence_path=rel, evidence_sha=report_hash
    )
    return gate_path, report_path


def test_valid_l1_ready_passes(tmp_path):
    gate_path, report_path = _valid(tmp_path)
    result = verify_l1_ready(_request(tmp_path, gate_path, report_path))
    assert result.ok, result.reasons


@pytest.mark.parametrize(
    "override,needle",
    [
        ({"expected_layer1_schema_hash": "9" * 64}, "schema hash"),
        ({"expected_profiler_config_hash": "9" * 64}, "configuration hash"),
        ({"expected_profiler_version": "other"}, "profiler version"),
        ({"expected_qualified_source_git_sha": "9" * 64}, "source commit"),
        ({"expected_qualified_source_tree_sha": "9" * 64}, "source tree"),
    ],
)
def test_mismatches_rejected(tmp_path, override, needle):
    gate_path, report_path = _valid(tmp_path)
    result = verify_l1_ready(_request(tmp_path, gate_path, report_path, **override))
    assert not result.ok
    assert any(needle in r for r in result.reasons), result.reasons


def test_missing_report_rejected(tmp_path):
    gate_path, report_path = _valid(tmp_path)
    import os

    os.remove(report_path)
    result = verify_l1_ready(_request(tmp_path, gate_path, report_path))
    assert not result.ok
    assert any("report is missing" in r for r in result.reasons)


def test_report_hash_mismatch_rejected(tmp_path):
    gate_path, report_path = _valid(tmp_path)
    # Tamper the report after the gate recorded its hash.
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write('{"tampered": true}')
    result = verify_l1_ready(_request(tmp_path, gate_path, report_path))
    assert not result.ok
    assert any("report hash mismatch" in r for r in result.reasons)


def test_evidence_tamper_rejected(tmp_path):
    gate_path, report_path = _valid(tmp_path)
    # The evidence points at the report; tampering it breaks the evidence hash too.
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write('{"tampered": true}')
    result = verify_l1_ready(_request(tmp_path, gate_path, report_path))
    assert not result.ok
    assert any("evidence hash mismatch" in r for r in result.reasons)


def test_wrong_gate_name_rejected(tmp_path):
    # A structurally valid PASS gate whose name is not L1-READY.
    report_path, report_hash = _write_report(tmp_path)
    gate = GateArtifact(
        gate_name="PROTOCOL-READY",
        status=GateStatus.PASS,
        engine_git_sha="abc",
        mandatory_checks=dict.fromkeys(required_checks_for("PROTOCOL-READY"), True),
        evidence=(
            EvidenceItem(description="e", path="reports/l1-report.json", sha256=report_hash),
        ),
        qualified_source_git_sha=SRC_SHA,
        qualified_source_tree_sha=TREE_SHA,
        qualification_tool_version="x",
        created_at=TS,
    )
    path = tmp_path / "gates" / "l1-ready.json"
    write_gate(gate, path)
    result = verify_l1_ready(_request(tmp_path, str(path), report_path))
    assert not result.ok
    assert any("gate_name" in r for r in result.reasons)


def test_non_pass_status_rejected(tmp_path):
    report_path, report_hash = _write_report(tmp_path)
    gate = GateArtifact(
        gate_name="L1-READY",
        status=GateStatus.HOLD,
        engine_git_sha="abc",
        mandatory_checks={"determinism": True},
        evidence=(
            EvidenceItem(description="e", path="reports/l1-report.json", sha256=report_hash),
        ),
        created_at=TS,
    )
    path = tmp_path / "gates" / "l1-ready.json"
    write_gate(gate, path)
    result = verify_l1_ready(_request(tmp_path, str(path), report_path))
    assert not result.ok
    assert any("not PASS" in r for r in result.reasons)
