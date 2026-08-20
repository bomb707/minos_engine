"""E5 provenance + leakage negatives for the FEATURE-VIEW-READY / FEATURE-MATRIX-FROZEN-1
qualification. Proves the gate verifier rejects sibling/ancestor/unrelated/wrong-tree/
self-binding sources, and that the production feature-view path structurally excludes
``test`` and offers no caller override.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.qualification import layer2_feature_view_runner as R

_ROOT = Path(".").resolve()
_GATE = R.FEATURE_VIEW_READY_GATE


def _all_true_gate(*, source: str, tree: str, self_bind: bool = False) -> GateArtifact:
    checks = dict.fromkeys(required_checks_for(_GATE), True)
    ev = (
        EvidenceItem(
            description="x", path=R.MIGRATION_0005_FILE, kind=EvidenceKind.FILE, sha256="a" * 64
        ),
    )
    gate = GateArtifact(
        gate_name=_GATE,
        status=GateStatus.PASS,
        engine_git_sha=source,
        input_hashes={"e4_evidence_commit": PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT},
        evidence=ev,
        mandatory_checks=checks,
        qualified_source_git_sha=source,
        qualified_source_tree_sha=tree,
        qualification_tool_version=R.E5_QUALIFIER_VERSION,
        created_at="2026-08-20T00:00:00+00:00",
    )
    if self_bind:
        gate = gate.model_copy(update={"qualified_source_git_sha": gate.gate_hash})
    return gate


def _c(gate: GateArtifact) -> dict[str, bool]:
    return R._contract_checks(_ROOT, gate, _GATE)


def test_wrong_tree_rejected() -> None:
    # a real commit but a mismatched tree
    gate = _all_true_gate(source=PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT, tree="0" * 40)
    assert not _c(gate)["qualified_source_tree_matches"]


def test_ancestor_source_does_not_descend_e4_evidence_rejected() -> None:
    # e07e586b is an ANCESTOR of the E4 evidence commit, so it cannot be a valid source.
    anc = "e07e586b39964eeb96f218cf21899bea65c8292b"
    tree = R.G.commit_tree_sha(_ROOT, anc) or ""
    checks = _c(_all_true_gate(source=anc, tree=tree))
    assert not checks["source_descends_e4_evidence"]


def test_unrelated_non_commit_source_rejected() -> None:
    gate = _all_true_gate(source="1234567890abcdef1234567890abcdef12345678", tree="0" * 40)
    assert not _c(gate)["qualified_source_present"]


def test_self_binding_rejected() -> None:
    gate = _all_true_gate(
        source=PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT,
        tree=R.G.commit_tree_sha(_ROOT, PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT) or "",
        self_bind=True,
    )
    assert not _c(gate)["not_self_binding"]


def test_matrix_gate_requires_pinned_hashes() -> None:
    # FEATURE-MATRIX-FROZEN-1 verify binds the committed identities to the accepted pins.
    from minos_engine.gates.verifier import write_gate

    checks = dict.fromkeys(required_checks_for(R.FEATURE_MATRIX_FROZEN_1_GATE), True)
    gate = GateArtifact(
        gate_name=R.FEATURE_MATRIX_FROZEN_1_GATE,
        status=GateStatus.PASS,
        engine_git_sha=PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT,
        input_hashes={"train_matrix_hash": "0" * 64},  # WRONG (not the accepted pin)
        evidence=(
            EvidenceItem(
                description="x", path=R.MIGRATION_0005_FILE, kind=EvidenceKind.FILE, sha256="a" * 64
            ),
        ),
        mandatory_checks=checks,
        qualified_source_git_sha=PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT,
        qualified_source_tree_sha=R.G.commit_tree_sha(_ROOT, PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT)
        or "",
        qualification_tool_version=R.E5_QUALIFIER_VERSION,
        created_at="2026-08-20T00:00:00+00:00",
    )
    tmp = _ROOT / "reports" / "_tmp_frozen_gate_test.json"
    tmp.parent.mkdir(exist_ok=True)
    try:
        write_gate(gate, tmp)
        res = R.verify_feature_matrix_frozen_1_gate(_ROOT, tmp)
        assert not res.ok and not res.checks["train_hash_bound_to_pin"]
    finally:
        tmp.unlink(missing_ok=True)


# ---- leakage: production feature-view path structurally excludes test + overrides ---- #
def test_verify_feature_view_rejects_test_partition() -> None:
    from minos_engine.storage.feature_view_verify import (
        FeatureViewVerificationError,
        verify_feature_view,
    )

    with pytest.raises(FeatureViewVerificationError, match="partition"):
        verify_feature_view(None, "test")  # type: ignore[arg-type]  # rejected before any DB use


def test_access_layer_has_no_test_entry_point() -> None:
    from minos_engine.storage import feature_view_access as ACC

    names = set(dir(ACC))
    assert "open_train_feature_view" in names and "open_validation_feature_view" in names
    assert not any("test" in n for n in names if n.startswith("open_"))


def test_production_verifier_has_no_caller_overrides() -> None:
    from minos_engine.storage.feature_view_verify import verify_feature_view

    params = set(inspect.signature(verify_feature_view).parameters)
    for forbidden in (
        "feature_set",
        "columns",
        "artifact_root",
        "artifact_path",
        "registry_hash",
        "snapshot_hash",
    ):
        assert forbidden not in params
