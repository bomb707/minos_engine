"""SPLIT-FROZEN-V2 gate: required checks, v1 SPLIT-FROZEN closure ancestry, tamper verify.

The v2 epoched split supersedes v1 within L2-C. The committed-gate + tamper tests skip
until the SPLIT-FROZEN-V2 gate artifact is produced (the two-commit evidence step); the
closure-ancestry tests run now against synthetic git graphs with the pinned v1 identities
monkeypatched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from minos_engine.gates.contracts import GateArtifact
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.qualification.layer2_split_v2_runner import (
    GATE_NAME,
    V2_MIGRATION_FILE,
    split_frozen_closure_checks,
    verify_split_frozen_v2_gate,
)
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "split-frozen-v2.json"


# --------------------------------------------------------------------------- #
# Required-check registration
# --------------------------------------------------------------------------- #
def test_split_frozen_v2_required_checks_registered() -> None:
    required = required_checks_for(GATE_NAME)
    for check in (
        "accepted_split_frozen_unchanged",
        "split_frozen_source_present",
        "split_frozen_source_tree_bound",
        "split_frozen_evidence_present",
        "v2_source_descends_split_frozen",
        "head_descends_v2_source",
        "epoch_manifest_verified",
        "canonical_epoch_manifest_hash_bound",
        "epoch1_inherits_v1_partitions_exactly",
        "epoch1_zero_transitions",
        "epoch1_test_cohort_preserved",
        "epoch1_validation_cohort_preserved",
        "ancestor_v1_registry_bound",
        "registry_snapshot_hash_bound",
        "split_policy_hash_bound",
        "epoch1_is_first_epoch",
        "v2_migration_immutable",
        "v2_migration_file_evidence_bound",
        "v2_migration_contract_bound",
        "alembic_head_includes_v2",
        "ci_asserts_head_0003",
        "total_sample_count_75",
        "partition_totals_50_10_15",
        "per_chromosome_10_2_3",
        "epoch_role_isolation_passed",
        "sealed_test_access_denied_passed",
        "validation_evaluator_only_passed",
        "trainer_view_no_features_passed",
        "parent_immutability_passed",
        "growth_new_samples_only_passed",
        "removal_replacement_rejected_passed",
        "truth_mutation_isolation_ok",
        "service_still_blocked",
    ):
        assert check in required, check


# --------------------------------------------------------------------------- #
# v1 SPLIT-FROZEN closure ancestry (synthetic git graphs; pinned identities patched)
# --------------------------------------------------------------------------- #
def _g(root: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _g(root, "init", "-q")
    _g(root, "config", "user.email", "t@e.com")
    _g(root, "config", "user.name", "t")


def _commit(root: Path, name: str) -> str:
    (root / name).write_text(name, encoding="utf-8")
    _g(root, "add", "-A")
    _g(root, "commit", "-q", "-m", name)
    return _g(root, "rev-parse", "HEAD")


def _tree(root: Path, sha: str) -> str:
    return _g(root, "rev-parse", f"{sha}^{{tree}}")


def _set_pre(mp: pytest.MonkeyPatch, *, src: str, src_tree: str, evi: str) -> None:
    mp.setattr(PRE, "SPLIT_FROZEN_SOURCE_COMMIT", src)
    mp.setattr(PRE, "SPLIT_FROZEN_SOURCE_TREE", src_tree)
    mp.setattr(PRE, "SPLIT_FROZEN_EVIDENCE_COMMIT", evi)


def _linear(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """split_frozen_source -> split_frozen_evidence -> v2_source (returns the three shas)."""
    _init(root)
    src = _commit(root, "sf_src")
    evi = _commit(root, "sf_evi")
    v2 = _commit(root, "v2_src")
    _set_pre(monkeypatch, src=src, src_tree=_tree(root, src), evi=evi)
    return src, evi, v2


def test_closure_valid_chain_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    _src, _evi, v2 = _linear(root, monkeypatch)
    head = _commit(root, "later_head")
    c = split_frozen_closure_checks(root, v2_source_ref=v2, head_ref=head)
    for k in (
        "split_frozen_source_present",
        "split_frozen_source_tree_bound",
        "split_frozen_evidence_present",
        "v2_source_descends_split_frozen",
        "head_descends_v2_source",
    ):
        assert c[k] is True, k


def test_missing_source_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    _linear(root, monkeypatch)
    c = split_frozen_closure_checks(root, v2_source_ref="0" * 40)
    assert c["v2_source_descends_split_frozen"] is False


def test_ancestor_source_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    src, _evi, _v2 = _linear(root, monkeypatch)
    # the v1 source predates the v1 evidence -> evidence not an ancestor of it
    c = split_frozen_closure_checks(root, v2_source_ref=src)
    assert c["v2_source_descends_split_frozen"] is False


def test_sibling_source_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    src, _evi, _v2 = _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "-b", "sib", src)  # branch off BEFORE the evidence
    sib = _commit(root, "sibling_src")
    c = split_frozen_closure_checks(root, v2_source_ref=sib)
    assert c["v2_source_descends_split_frozen"] is False


def test_unrelated_orphan_source_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "--orphan", "orphan")
    orph = _commit(root, "orphan_root")
    c = split_frozen_closure_checks(root, v2_source_ref=orph)
    assert c["v2_source_descends_split_frozen"] is False


def test_merge_head_does_not_launder_sibling_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A later merge HEAD that descends BOTH the v1 evidence and a sibling v2 source must
    # NOT make that sibling source acceptable.
    root = tmp_path / "r"
    src, evi, _v2 = _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "-b", "sib", src)  # sibling lineage from before evidence
    sib = _commit(root, "sibling_src")
    _g(root, "checkout", "-q", evi)
    _g(root, "merge", "-q", "--no-ff", "-m", "merge_sib", "sib")
    merge_head = _g(root, "rev-parse", "HEAD")
    c = split_frozen_closure_checks(root, v2_source_ref=sib, head_ref=merge_head)
    assert c["head_descends_v2_source"] is True  # HEAD descends the sibling source...
    assert c["v2_source_descends_split_frozen"] is False  # ...but it is not from v1


def test_head_not_descending_source_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    _src, evi, v2 = _linear(root, monkeypatch)
    _commit(root, "later")
    c = split_frozen_closure_checks(root, v2_source_ref=v2, head_ref=evi)
    assert c["head_descends_v2_source"] is False


# --------------------------------------------------------------------------- #
# Pinned v1 identities are the real accepted ones + ancestry holds on this repo
# --------------------------------------------------------------------------- #
def test_pinned_v1_identities_are_accepted() -> None:
    assert PRE.SPLIT_FROZEN_GATE_HASH == (
        "5520328868f408fe705a9d6618e3d67c081fa4e0aaa8dd764bb933aea866c702"
    )
    assert len(PRE.SPLIT_FROZEN_SOURCE_COMMIT) == 40
    assert len(PRE.SPLIT_FROZEN_EVIDENCE_COMMIT) == 40


# --------------------------------------------------------------------------- #
# Committed-gate verification + tamper (skips until the two-commit gate is produced)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_committed_split_v2_gate_verifies() -> None:
    result = verify_split_frozen_v2_gate(REPO_ROOT, _GATE, require_descends=True)
    assert result.ok, result.reasons


def _tamper(tmp_path: Path, mutate) -> Path:
    raw = json.loads(_GATE.read_text(encoding="utf-8"))
    mutate(raw)
    raw["gate_hash"] = ""
    gate = GateArtifact.model_validate(raw)
    path = tmp_path / "gates" / "split-frozen-v2.json"
    return write_gate(gate, path)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_tampered_epoch_manifest_hash_rejected(tmp_path: Path) -> None:
    def mut(raw: dict) -> None:
        raw["input_hashes"]["canonical_epoch_manifest_hash"] = "0" * 64

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok
    assert any("canonical_epoch_manifest_hash_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_tampered_registry_snapshot_hash_rejected(tmp_path: Path) -> None:
    def mut(raw: dict) -> None:
        raw["input_hashes"]["registry_snapshot_hash"] = "0" * 64

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok
    assert any("registry_snapshot_hash_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_tampered_ancestor_v1_hash_rejected(tmp_path: Path) -> None:
    def mut(raw: dict) -> None:
        raw["input_hashes"]["ancestor_v1_dataset_registry_hash"] = "0" * 64

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok
    assert any("ancestor_v1_registry_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_tampered_transition_count_rejected(tmp_path: Path) -> None:
    """A gate claiming nonzero transitions can never verify."""

    def mut(raw: dict) -> None:
        raw["input_hashes"]["transition_count"] = "36"
        raw["mandatory_checks"]["epoch1_zero_transitions"] = True  # lying check

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_tampered_split_policy_hash_rejected(tmp_path: Path) -> None:
    def mut(raw: dict) -> None:
        raw["input_hashes"]["split_policy_hash"] = "0" * 64

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok
    assert any("split_policy_hash_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_only_migration_file_hash_changed_rejected(tmp_path: Path) -> None:
    def mut(raw: dict) -> None:
        raw["input_hashes"]["v2_migration_file_hash"] = "1" * 64

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok
    assert any("v2_migration_file_evidence_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_both_migration_hashes_changed_consistently_rejected(tmp_path: Path) -> None:
    from minos_engine.storage.l2c_split_v2_migration_contract import l2c_split_v2_contract_hash

    x = "3" * 64

    def mut(raw: dict) -> None:
        for e in raw["evidence"]:
            if e["path"] == V2_MIGRATION_FILE:
                e["sha256"] = x
        raw["input_hashes"]["v2_migration_file_hash"] = x
        raw["input_hashes"]["v2_migration_contract_hash"] = l2c_split_v2_contract_hash(x)

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok
    assert any("v2_migration_evidence_matches_source_blob" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_migration_evidence_duplicated_rejected(tmp_path: Path) -> None:
    def mut(raw: dict) -> None:
        item = next(e for e in raw["evidence"] if e["path"] == V2_MIGRATION_FILE)
        raw["evidence"].append(dict(item))

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok
    assert any("v2_migration_evidence_present" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN-V2 gate produced in evidence commit")
def test_wrong_qualified_source_rejected(tmp_path: Path) -> None:
    def mut(raw: dict) -> None:
        raw["qualified_source_git_sha"] = "0" * 40

    result = verify_split_frozen_v2_gate(REPO_ROOT, _tamper(tmp_path, mut), require_descends=False)
    assert not result.ok
