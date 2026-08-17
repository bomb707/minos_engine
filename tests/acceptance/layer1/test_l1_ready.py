"""Acceptance — the real L1-READY gate verifies and keeps Layer 2 blocked.

The committed ``gates/l1-ready.json`` is produced in the Layer 1 Commit B. Before
it exists (e.g. during the Commit A qualification run) these checks skip; once it
exists they verify the full chain and confirm Layer 2 authorization.
"""

from __future__ import annotations

import pytest
from tests.conftest import REPO_ROOT

from minos_engine.common.errors import StageNotReadyError
from minos_engine.layer1.prerequisites import verify_twin_ready_prerequisite
from minos_engine.layer2.service import Layer2Service

_GATE = REPO_ROOT / "gates" / "l1-ready.json"
_REPORT = REPO_ROOT / "reports" / "LAYER1_QUALIFICATION_REPORT.md"


def test_twin_prerequisite_accepted():
    assert verify_twin_ready_prerequisite(REPO_ROOT).ok


def test_layer2_service_still_blocked():
    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


@pytest.mark.skipif(not _GATE.exists(), reason="L1-READY gate is produced in Commit B")
def test_committed_l1_ready_gate_verifies():
    from minos_engine.qualification.layer1_runner import verify_l1_ready_gate

    result = verify_l1_ready_gate(REPO_ROOT, _GATE, require_descends=False)
    assert result.ok, result.reasons


@pytest.mark.skipif(not _GATE.exists(), reason="L1-READY gate is produced in Commit B")
def test_committed_l1_ready_gate_verifies_with_descent():
    # HEAD must properly descend from the qualified source commit — the repaired
    # ancestry supports the artifact commit and any later genuine descendant.
    from minos_engine.qualification.layer1_runner import verify_l1_ready_gate

    result = verify_l1_ready_gate(REPO_ROOT, _GATE, require_descends=True)
    assert result.ok, result.reasons
    assert result.checks["commit_b_descends_a"] is True


@pytest.mark.skipif(not _GATE.exists(), reason="L1-READY gate is produced in Commit B")
def test_committed_l1_ready_gate_authorizes_layer2_entry():
    # The hardened entry gate pins all accepted identities; the caller supplies
    # only the repo root. This proves the full accepted history chain.
    from minos_engine.layer2.entry_gate import EntryGateRequest, verify_l2_entry_gate

    result = verify_l2_entry_gate(EntryGateRequest(repo_root=str(REPO_ROOT)))
    assert result.ok, result.reasons
    assert result.checks["head_descends_owner"] is True
    assert result.checks["artifact_proper_descends_source"] is True
