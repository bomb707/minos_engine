"""Stage-gate behavior: Layer 1 not ready, Layer 2 blocked, entry gate rejects."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import ContractValidationError, GateError, StageNotReadyError
from minos_engine.layer1.service import Layer1Service
from minos_engine.layer2.entry_gate import EntryGateRequest, require_l1_ready, verify_l1_ready
from minos_engine.layer2.service import Layer2Service
from minos_engine.schema_registry import validate_against


def test_layer1_now_implemented():
    # Layer 1 is implemented at this stage: the service exposes the real profiling
    # entry point instead of raising StageNotReadyError.
    assert hasattr(Layer1Service, "profile")
    assert hasattr(Layer1Service, "analyze")


def test_layer2_blocked():
    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_entry_gate_rejects_missing_l1_ready(tmp_path):
    req = EntryGateRequest(
        l1_ready_path=str(tmp_path / "l1-ready.json"),
        qualification_report_path=str(tmp_path / "l1-report.json"),
        expected_layer1_schema_hash="a" * 64,
        expected_profiler_config_hash="b" * 64,
        expected_profiler_version="l1-profiler-v1",
    )
    result = verify_l1_ready(req)
    assert not result.ok
    assert any("missing" in r for r in result.reasons)
    with pytest.raises(GateError):
        require_l1_ready(req)


def test_incompatible_schema_version_rejected():
    bad = {"schema_version": "gate-artifact-v99", "gate_name": "x"}
    with pytest.raises(ContractValidationError):
        validate_against("gate-artifact-v1", bad)
