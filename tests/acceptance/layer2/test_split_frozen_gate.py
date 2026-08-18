"""SPLIT-FROZEN gate: required checks, DB-READY closure ancestry, and tamper verification."""

from __future__ import annotations

import json
import subprocess

import pytest

from minos_engine.gates.contracts import GateArtifact
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.qualification.layer2_split_runner import (
    GATE_NAME,
    L2C_MIGRATION_FILE,
    db_ready_closure_checks,
    verify_split_frozen_gate,
)
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "split-frozen.json"


# --------------------------------------------------------------------------- #
# Required-check registration
# --------------------------------------------------------------------------- #
def test_split_frozen_required_checks_registered():
    required = required_checks_for(GATE_NAME)
    for check in (
        "accepted_db_ready_unchanged",
        "db_ready_source_present",
        "db_ready_source_tree_bound",
        "db_ready_evidence_present",
        "l2c_source_descends_db_ready",
        "head_descends_l2c_source",
        "manifest_verified",
        "canonical_manifest_hash_bound",
        "dataset_registry_hash_bound",
        "split_policy_hash_bound",
        "local_input_inventory_hash_bound",
        "l2c_migration_immutable",
        "l2c_migration_file_evidence_bound",
        "l2c_migration_contract_bound",
        "alembic_head_is_l2c",
        "total_sample_count_75",
        "partition_totals_50_10_15",
        "per_chromosome_10_2_3",
        "role_isolation_passed",
        "truth_mutation_isolation_ok",
        "service_still_blocked",
    ):
        assert check in required, check


# --------------------------------------------------------------------------- #
# DB-READY closure ancestry (synthetic git graphs; pinned identities monkeypatched)
# --------------------------------------------------------------------------- #
def _g(root, *a):
    return subprocess.run(
        ["git", "-C", str(root), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    _g(root, "init", "-q")
    _g(root, "config", "user.email", "t@e.com")
    _g(root, "config", "user.name", "t")


def _commit(root, name):
    (root / name).write_text(name, encoding="utf-8")
    _g(root, "add", "-A")
    _g(root, "commit", "-q", "-m", name)
    return _g(root, "rev-parse", "HEAD")


def _tree(root, sha):
    return _g(root, "rev-parse", f"{sha}^{{tree}}")


def _set_pre(mp, *, src, src_tree, evi):
    mp.setattr(PRE, "DB_READY_SOURCE_COMMIT", src)
    mp.setattr(PRE, "DB_READY_SOURCE_TREE", src_tree)
    mp.setattr(PRE, "DB_READY_EVIDENCE_COMMIT", evi)


def _linear(root, monkeypatch):
    """db_ready_source -> db_ready_evidence -> l2c_source (returns the three shas)."""
    _init(root)
    src = _commit(root, "dbready_src")
    evi = _commit(root, "dbready_evi")
    l2c = _commit(root, "l2c_src")
    _set_pre(monkeypatch, src=src, src_tree=_tree(root, src), evi=evi)
    return src, evi, l2c


def test_closure_valid_chain_passes(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _src, _evi, l2c = _linear(root, monkeypatch)
    head = _commit(root, "later_head")
    c = db_ready_closure_checks(root, l2c_source_ref=l2c, head_ref=head)
    for k in (
        "db_ready_source_present",
        "db_ready_source_tree_bound",
        "db_ready_evidence_present",
        "l2c_source_descends_db_ready",
        "head_descends_l2c_source",
    ):
        assert c[k] is True, k


def test_missing_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _linear(root, monkeypatch)
    c = db_ready_closure_checks(root, l2c_source_ref="0" * 40)
    assert c["l2c_source_descends_db_ready"] is False


def test_ancestor_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    src, _evi, _l2c = _linear(root, monkeypatch)
    # the DB-READY source predates the DB-READY evidence -> evidence not an ancestor of it
    c = db_ready_closure_checks(root, l2c_source_ref=src)
    assert c["l2c_source_descends_db_ready"] is False


def test_sibling_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    src, _evi, _l2c = _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "-b", "sib", src)  # branch off BEFORE the evidence
    sib = _commit(root, "sibling_src")
    c = db_ready_closure_checks(root, l2c_source_ref=sib)
    assert c["l2c_source_descends_db_ready"] is False


def test_unrelated_orphan_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "--orphan", "orphan")
    orph = _commit(root, "orphan_root")
    c = db_ready_closure_checks(root, l2c_source_ref=orph)
    assert c["l2c_source_descends_db_ready"] is False


def test_merge_head_does_not_launder_sibling_source(tmp_path, monkeypatch):
    # MANDATORY: a later merge HEAD that descends BOTH the DB-READY evidence and a
    # sibling L2-C source must NOT make that sibling source acceptable.
    root = tmp_path / "r"
    _src, evi, _l2c = _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "-b", "sib", _src)  # sibling lineage from before evidence
    sib = _commit(root, "sibling_src")
    _g(root, "checkout", "-q", evi)
    _g(root, "merge", "-q", "--no-ff", "-m", "merge_sib", "sib")
    merge_head = _g(root, "rev-parse", "HEAD")
    assert (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", evi, merge_head]
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", sib, merge_head]
        ).returncode
        == 0
    )
    c = db_ready_closure_checks(root, l2c_source_ref=sib, head_ref=merge_head)
    assert c["head_descends_l2c_source"] is True  # HEAD descends the sibling source...
    assert c["l2c_source_descends_db_ready"] is False  # ...but it is not from DB-READY


def test_head_not_descending_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _src, evi, l2c = _linear(root, monkeypatch)
    _commit(root, "later")
    c = db_ready_closure_checks(root, l2c_source_ref=l2c, head_ref=evi)
    assert c["head_descends_l2c_source"] is False


# --------------------------------------------------------------------------- #
# Committed-gate verification + tamper (skips until Commit V adds the gate)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_committed_split_gate_verifies():
    result = verify_split_frozen_gate(REPO_ROOT, _GATE, require_descends=True)
    assert result.ok, result.reasons


def _tamper(tmp_path, mutate) -> str:
    raw = json.loads(_GATE.read_text(encoding="utf-8"))
    mutate(raw)
    raw["gate_hash"] = ""
    gate = GateArtifact.model_validate(raw)
    path = tmp_path / "gates" / "split-frozen.json"
    write_gate(gate, path)
    return str(path)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_tampered_manifest_hash_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"]["canonical_manifest_hash"] = "0" * 64

    result = verify_split_frozen_gate(
        REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False
    )
    assert not result.ok
    assert any("canonical_manifest_hash_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_tampered_registry_hash_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"]["dataset_registry_hash"] = "0" * 64

    result = verify_split_frozen_gate(
        REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False
    )
    assert not result.ok
    assert any("dataset_registry_hash_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_tampered_split_policy_hash_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"]["split_policy_hash"] = "0" * 64

    result = verify_split_frozen_gate(
        REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False
    )
    assert not result.ok
    assert any("split_policy_hash_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_only_migration_file_hash_changed_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"]["l2c_migration_file_hash"] = "1" * 64

    result = verify_split_frozen_gate(
        REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False
    )
    assert not result.ok
    assert any("l2c_migration_file_evidence_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_only_migration_contract_hash_changed_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"]["l2c_migration_contract_hash"] = "2" * 64

    result = verify_split_frozen_gate(
        REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False
    )
    assert not result.ok
    assert any("l2c_migration_contract_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_both_migration_hashes_changed_consistently_rejected(tmp_path):
    from pathlib import Path

    from minos_engine.storage.l2c_migration_contract import l2c_contract_hash

    x = "3" * 64

    def mut(raw):
        # change the evidence item, the input file hash, AND the contract consistently.
        for e in raw["evidence"]:
            if e["path"] == L2C_MIGRATION_FILE:
                e["sha256"] = x
        raw["input_hashes"]["l2c_migration_file_hash"] = x
        raw["input_hashes"]["l2c_migration_contract_hash"] = l2c_contract_hash(x)

    result = verify_split_frozen_gate(
        REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False
    )
    assert not result.ok
    # fails because the evidence hash no longer equals the committed source blob.
    assert any("l2c_migration_evidence_matches_source_blob" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_migration_evidence_duplicated_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        item = next(e for e in raw["evidence"] if e["path"] == L2C_MIGRATION_FILE)
        raw["evidence"].append(dict(item))

    result = verify_split_frozen_gate(
        REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False
    )
    assert not result.ok
    assert any("l2c_migration_evidence_present" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="SPLIT-FROZEN gate produced in Commit V")
def test_wrong_qualified_source_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["qualified_source_git_sha"] = "0" * 40

    result = verify_split_frozen_gate(
        REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False
    )
    assert not result.ok
