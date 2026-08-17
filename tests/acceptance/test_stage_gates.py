"""Stage-gate behavior: Layer 1 not ready, Layer 2 blocked, entry gate rejects."""

from __future__ import annotations

import json

import pytest

from minos_engine.common.errors import ContractValidationError, GateError, StageNotReadyError
from minos_engine.layer1.service import Layer1Service
from minos_engine.layer2.entry_gate import EntryGateRequest, require_l1_ready, verify_l1_ready
from minos_engine.layer2.service import Layer2Service
from minos_engine.schema_registry import validate_against


def test_layer1_not_implemented():
    with pytest.raises(StageNotReadyError):
        Layer1Service().analyze(None)  # type: ignore[arg-type]


def test_layer2_blocked():
    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_entry_gate_rejects_missing_l1_ready(tmp_path):
    req = EntryGateRequest(
        l1_ready_path=str(tmp_path / "l1-ready.json"),
        expected_layer1_schema_hash="a" * 64,
        expected_profiler_config_hash="b" * 64,
    )
    result = verify_l1_ready(req)
    assert not result.ok
    assert any("missing" in r for r in result.reasons)
    with pytest.raises(GateError):
        require_l1_ready(req)


def test_entry_gate_rejects_hash_mismatch(tmp_path):
    # A syntactically valid PASS gate but with mismatched hashes / no report.
    gate = {
        "schema_version": "gate-artifact-v1",
        "gate_name": "L1-READY",
        "status": "PASS",
        "engine_git_sha": "abc",
        "input_hashes": {"layer1_schema_hash": "z" * 64, "profiler_config_hash": "y" * 64},
        "evidence": [{"description": "report", "path": "reports/l1.json"}],
        "mandatory_checks": {"determinism": True},
        "created_at": "2026-08-17T12:00:00+00:00",
        "gate_hash": "",
    }
    from minos_engine.gates.contracts import GateArtifact

    materialized = GateArtifact.model_validate(gate).model_dump(mode="json")
    path = tmp_path / "l1-ready.json"
    path.write_text(json.dumps(materialized))
    req = EntryGateRequest(
        l1_ready_path=str(path),
        expected_layer1_schema_hash="a" * 64,
        expected_profiler_config_hash="b" * 64,
    )
    result = verify_l1_ready(req)
    assert not result.ok
    assert any("mismatch" in r for r in result.reasons)


def test_incompatible_schema_version_rejected():
    bad = {"schema_version": "gate-artifact-v99", "gate_name": "x"}
    with pytest.raises(ContractValidationError):
        validate_against("gate-artifact-v1", bad)
