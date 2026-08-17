"""Hardened Layer 2 entry-gate — positive, content negatives, and path security.

The positive case runs against the committed ``gates/l1-ready.json`` and the real
git history. Content negatives write a tampered gate/report at the canonical path
inside a throwaway ``repo_root`` (so the targeted content reason surfaces; the
absence of git history there is an accepted extra reason). Path-security tests use
the real repo root with constrained overrides. Synthetic-repo git-ancestry
negatives live in ``tests/acceptance/layer2/test_entry_gate_git.py``.
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


def _tmp_repo(tmp_path, raw: dict, *, report: bytes | None = b"placeholder") -> EntryGateRequest:
    """Write a (possibly tampered) gate at the canonical path in a throwaway root."""
    raw = dict(raw)
    raw["gate_hash"] = ""  # force canonical recompute over the mutated content
    gate = GateArtifact.model_validate(raw)
    write_gate(gate, tmp_path / "gates" / "l1-ready.json")
    if report is not None:
        rp = tmp_path / "reports" / "LAYER1_QUALIFICATION_REPORT.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_bytes(report)
    return EntryGateRequest(repo_root=str(tmp_path))


# --------------------------------------------------------------------------- #
# Positive
# --------------------------------------------------------------------------- #
def test_real_entry_gate_passes():
    result = verify_l2_entry_gate(EntryGateRequest(repo_root=str(REPO_ROOT)))
    assert result.ok, result.reasons
    assert all(result.checks.values())
    require_l2_entry_gate(EntryGateRequest(repo_root=str(REPO_ROOT)))  # does not raise


def test_canonical_override_matches_default():
    result = verify_l2_entry_gate(
        EntryGateRequest(
            repo_root=str(REPO_ROOT),
            l1_ready_path="gates/l1-ready.json",
            qualification_report_path="reports/LAYER1_QUALIFICATION_REPORT.md",
        )
    )
    assert result.ok, result.reasons


# --------------------------------------------------------------------------- #
# Gate-content negatives (tampered canonical gate in a throwaway root)
# --------------------------------------------------------------------------- #
def test_self_consistent_but_unaccepted_gate_rejected(tmp_path):
    raw = _real_raw()
    raw["mandatory_checks"] = dict(raw["mandatory_checks"], extra_supplemental_check=True)
    result = verify_l2_entry_gate(_tmp_repo(tmp_path, raw))
    assert not result.ok
    assert "GATE_HASH_NOT_ACCEPTED" in result.reasons


def test_wrong_gate_name_rejected(tmp_path):
    raw = _real_raw()
    raw["gate_name"] = "L1-READY-IMPOSTER"  # unregistered name; no required-check set
    result = verify_l2_entry_gate(_tmp_repo(tmp_path, raw))
    assert not result.ok
    assert "GATE_NAME_MISMATCH" in result.reasons


def test_layer1_schema_hash_mismatch_rejected(tmp_path):
    raw = _real_raw()
    raw["input_hashes"] = dict(raw["input_hashes"], layer1_schema_hash="9" * 64)
    result = verify_l2_entry_gate(_tmp_repo(tmp_path, raw))
    assert not result.ok
    assert "LAYER1_SCHEMA_HASH_MISMATCH" in result.reasons


def test_profiler_version_mismatch_rejected(tmp_path):
    raw = _real_raw()
    raw["input_hashes"] = dict(raw["input_hashes"], profiler_version="other")
    result = verify_l2_entry_gate(_tmp_repo(tmp_path, raw))
    assert not result.ok
    assert "PROFILER_VERSION_MISMATCH" in result.reasons


def test_qualified_source_tree_mismatch_rejected(tmp_path):
    raw = _real_raw()
    raw["qualified_source_tree_sha"] = "4" * 40
    result = verify_l2_entry_gate(_tmp_repo(tmp_path, raw))
    assert not result.ok
    assert "QUALIFIED_SOURCE_TREE_MISMATCH" in result.reasons


def test_canonical_hash_tamper_rejected(tmp_path):
    raw = _real_raw()
    raw["gate_hash"] = "0" * 64  # inconsistent with content, not recomputed
    path = tmp_path / "gates" / "l1-ready.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    result = verify_l2_entry_gate(EntryGateRequest(repo_root=str(tmp_path)))
    assert not result.ok
    assert "CANONICAL_HASH_INVALID" in result.reasons


def test_unparseable_gate_rejected(tmp_path):
    path = tmp_path / "gates" / "l1-ready.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    result = verify_l2_entry_gate(EntryGateRequest(repo_root=str(tmp_path)))
    assert not result.ok
    assert "GATE_UNPARSEABLE" in result.reasons


def test_missing_gate_rejected(tmp_path):
    result = verify_l2_entry_gate(EntryGateRequest(repo_root=str(tmp_path)))
    assert not result.ok
    assert "L1_READY_MISSING" in result.reasons


def test_tampered_report_rejected(tmp_path):
    result = verify_l2_entry_gate(_tmp_repo(tmp_path, _real_raw(), report=b"tampered"))
    assert not result.ok
    assert "QUALIFICATION_REPORT_HASH_MISMATCH" in result.reasons


def test_missing_report_rejected(tmp_path):
    result = verify_l2_entry_gate(_tmp_repo(tmp_path, _real_raw(), report=None))
    assert not result.ok
    assert "QUALIFICATION_REPORT_MISSING" in result.reasons


# --------------------------------------------------------------------------- #
# Path security — external paths rejected even with accepted-hash bytes
# --------------------------------------------------------------------------- #
def test_external_absolute_gate_path_rejected():
    result = verify_l2_entry_gate(
        EntryGateRequest(repo_root=str(REPO_ROOT), l1_ready_path=str(_GATE))
    )
    assert not result.ok
    assert "EXTERNAL_PATH_ABSOLUTE" in result.reasons


def test_external_absolute_report_path_rejected():
    result = verify_l2_entry_gate(
        EntryGateRequest(repo_root=str(REPO_ROOT), qualification_report_path=str(_REPORT))
    )
    assert not result.ok
    assert "EXTERNAL_PATH_ABSOLUTE" in result.reasons


def test_parent_escape_override_rejected():
    result = verify_l2_entry_gate(
        EntryGateRequest(repo_root=str(REPO_ROOT), l1_ready_path="../l1-ready.json")
    )
    assert not result.ok
    assert "EXTERNAL_PATH_ESCAPE" in result.reasons


def test_noncanonical_override_rejected():
    result = verify_l2_entry_gate(
        EntryGateRequest(repo_root=str(REPO_ROOT), l1_ready_path="gates/other.json")
    )
    assert not result.ok
    assert "EXTERNAL_PATH_NOT_CANONICAL" in result.reasons


def test_symlink_escape_gate_rejected(tmp_path):
    # Canonical gate path is a symlink pointing outside the repo root (sibling dir).
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "l1-ready.json").write_text(_GATE.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "gates").mkdir()
    (repo / "gates" / "l1-ready.json").symlink_to(outside / "l1-ready.json")
    result = verify_l2_entry_gate(EntryGateRequest(repo_root=str(repo)))
    assert not result.ok
    assert "EXTERNAL_PATH_SYMLINK" in result.reasons


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
        require_l2_entry_gate(EntryGateRequest(repo_root=str(tmp_path)))
