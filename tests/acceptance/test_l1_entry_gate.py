"""Hardened Layer 2 entry-gate — content negatives against the real repository.

The positive case and gate-content negatives run against the committed
``gates/l1-ready.json`` and the real git history. The verifier pins all accepted
identities (callers cannot supply them), so negatives are produced by tampering a
*copy* of the gate/report and pointing the locator at it while the real repo
supplies the (passing) git-ancestry chain. Synthetic-repo git-ancestry negatives
live in ``tests/acceptance/layer2/test_entry_gate_git.py``.
"""

from __future__ import annotations

import json

import pytest

from minos_engine.common.errors import GateError
from minos_engine.gates.contracts import GateArtifact
from minos_engine.gates.verifier import write_gate
from minos_engine.layer2.entry_gate import (
    EntryGateRequest,
    require_l2_entry_gate,
    verify_l2_entry_gate,
)
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "l1-ready.json"
_REPORT = REPO_ROOT / "reports" / "LAYER1_QUALIFICATION_REPORT.md"
pytestmark = pytest.mark.skipif(not _GATE.exists(), reason="L1-READY gate produced in Commit B")


def _real_raw() -> dict:
    return json.loads(_GATE.read_text(encoding="utf-8"))


def _write_variant(tmp_path, raw: dict) -> str:
    raw = dict(raw)
    raw["gate_hash"] = ""  # force canonical recompute over the mutated content
    gate = GateArtifact.model_validate(raw)
    path = tmp_path / "gates" / "l1-ready.json"
    write_gate(gate, path)
    return str(path)


def _request(**overrides) -> EntryGateRequest:
    kwargs = {"repo_root": str(REPO_ROOT)}
    kwargs.update(overrides)
    return EntryGateRequest(**kwargs)


# --------------------------------------------------------------------------- #
# Positive
# --------------------------------------------------------------------------- #
def test_real_entry_gate_passes():
    result = verify_l2_entry_gate(_request())
    assert result.ok, result.reasons
    assert all(result.checks.values())
    require_l2_entry_gate(_request())  # does not raise


# --------------------------------------------------------------------------- #
# Gate-content negatives (real repo, tampered copy)
# --------------------------------------------------------------------------- #
def test_self_consistent_but_unaccepted_gate_rejected(tmp_path):
    raw = _real_raw()
    # Add a supplemental (true) check: valid PASS gate, new canonical hash != accepted.
    # (created_at is excluded from the hash, so it cannot be used to perturb identity.)
    raw["mandatory_checks"] = dict(raw["mandatory_checks"], extra_supplemental_check=True)
    gate_path = _write_variant(tmp_path, raw)
    result = verify_l2_entry_gate(_request(l1_ready_path=gate_path))
    assert not result.ok
    assert "GATE_HASH_NOT_ACCEPTED" in result.reasons


def test_wrong_gate_name_rejected(tmp_path):
    raw = _real_raw()
    raw["gate_name"] = "L1-READY-IMPOSTER"  # unregistered name; no required-check set
    gate_path = _write_variant(tmp_path, raw)
    result = verify_l2_entry_gate(_request(l1_ready_path=gate_path))
    assert not result.ok
    assert "GATE_NAME_MISMATCH" in result.reasons


def test_layer1_schema_hash_mismatch_rejected(tmp_path):
    raw = _real_raw()
    raw["input_hashes"] = dict(raw["input_hashes"], layer1_schema_hash="9" * 64)
    gate_path = _write_variant(tmp_path, raw)
    result = verify_l2_entry_gate(_request(l1_ready_path=gate_path))
    assert not result.ok
    assert "LAYER1_SCHEMA_HASH_MISMATCH" in result.reasons


def test_profiler_version_mismatch_rejected(tmp_path):
    raw = _real_raw()
    raw["input_hashes"] = dict(raw["input_hashes"], profiler_version="other")
    gate_path = _write_variant(tmp_path, raw)
    result = verify_l2_entry_gate(_request(l1_ready_path=gate_path))
    assert not result.ok
    assert "PROFILER_VERSION_MISMATCH" in result.reasons


def test_qualified_source_tree_mismatch_rejected(tmp_path):
    raw = _real_raw()
    raw["qualified_source_tree_sha"] = "4" * 40
    gate_path = _write_variant(tmp_path, raw)
    result = verify_l2_entry_gate(_request(l1_ready_path=gate_path))
    assert not result.ok
    assert "QUALIFIED_SOURCE_TREE_MISMATCH" in result.reasons


def test_canonical_hash_tamper_rejected(tmp_path):
    raw = _real_raw()
    raw["gate_hash"] = "0" * 64  # inconsistent with content, not recomputed
    path = tmp_path / "gates" / "l1-ready.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    result = verify_l2_entry_gate(_request(l1_ready_path=str(path)))
    assert not result.ok
    assert "CANONICAL_HASH_INVALID" in result.reasons


def test_unparseable_gate_rejected(tmp_path):
    path = tmp_path / "gates" / "l1-ready.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    result = verify_l2_entry_gate(_request(l1_ready_path=str(path)))
    assert not result.ok
    assert "GATE_UNPARSEABLE" in result.reasons


def test_missing_gate_rejected(tmp_path):
    result = verify_l2_entry_gate(_request(l1_ready_path=str(tmp_path / "nope.json")))
    assert not result.ok
    assert "L1_READY_MISSING" in result.reasons


def test_tampered_report_rejected(tmp_path):
    bad = tmp_path / "report.md"
    bad.write_text("tampered", encoding="utf-8")
    result = verify_l2_entry_gate(_request(qualification_report_path=str(bad)))
    assert not result.ok
    assert "QUALIFICATION_REPORT_HASH_MISMATCH" in result.reasons


def test_missing_report_rejected(tmp_path):
    result = verify_l2_entry_gate(_request(qualification_report_path=str(tmp_path / "absent.md")))
    assert not result.ok
    assert "QUALIFICATION_REPORT_MISSING" in result.reasons


# --------------------------------------------------------------------------- #
# Request shape — callers cannot weaken or override accepted identities
# --------------------------------------------------------------------------- #
def test_request_rejects_identity_override():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        EntryGateRequest(repo_root=".", expected_l1_gate_hash="a" * 64)  # type: ignore[call-arg]


def test_request_requires_repo_root():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        EntryGateRequest()  # type: ignore[call-arg]


def test_require_raises_on_failure(tmp_path):
    with pytest.raises(GateError):
        require_l2_entry_gate(_request(l1_ready_path=str(tmp_path / "nope.json")))
