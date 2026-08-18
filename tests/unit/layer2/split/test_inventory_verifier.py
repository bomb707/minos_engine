"""Independent inventory verification, safe-path policy, and evidence-payload aggregate.

These exercise the hermetic, git-independent pieces of the SPLIT-FROZEN closure:
``verify_inventory`` (recompute-from-entries + frozen-manifest correspondence + safe
paths + leakage isolation), ``_inv_path_safe``, ``_unique_evidence``, and the
non-circular ``evidence_payload_hash``.
"""

from __future__ import annotations

import copy

import pytest

from minos_engine.gates.contracts import EvidenceItem, EvidenceKind
from minos_engine.layer2.split.contracts import LocalInputEntry, LocalInputInventory
from minos_engine.qualification.layer2_split_runner import (
    _inv_path_safe,
    _unique_evidence,
    evidence_payload_hash,
    verify_inventory,
)
from tests.layer2c_synth import synthetic_inventory, synthetic_manifest

_H = "a" * 64


def _inv_dict() -> dict:
    return copy.deepcopy(synthetic_inventory().to_canonical())


def _rehash(inv_raw: dict) -> dict:
    """Rebuild the inventory from entries so the embedded hash is consistent again."""
    entries = tuple(LocalInputEntry(**e) for e in inv_raw["entries"])
    return LocalInputInventory(entries=entries).to_canonical()


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_verify_inventory_accepts_canonical_pair():
    result = verify_inventory(_inv_dict(), synthetic_manifest())
    assert result.ok, result.reasons
    assert result.embedded_matches
    for check in (
        "inventory_contract_valid",
        "inventory_hash_recomputed",
        "inventory_manifest_identity_bound",
        "inventory_paths_safe",
        "inventory_truth_mutation_isolation_ok",
    ):
        assert result.checks[check] is True, check


# --------------------------------------------------------------------------- #
# Inventory hash is recomputed, never self-trusted
# --------------------------------------------------------------------------- #
def test_path_changed_with_old_hash_rejected():
    raw = _inv_dict()
    raw["entries"][0]["bam_relpath"] = "practice/round_zzz/other.bam"
    # embedded inventory_hash is left stale -> the frozen contract recomputes and
    # rejects the mismatch (the embedded field is never trusted as-is).
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks.get("inventory_contract_valid") is False


def test_consistent_safe_path_substitution_rejected_by_identity():
    # A benign, still-safe relative-path change with a consistently recomputed hash
    # passes the inventory's own recomputation (embedded_matches True) — but it is
    # rejected because the path no longer equals the value DERIVED from the entry's
    # manifest-bound identity. This is the closure of the final defect.
    raw = _inv_dict()
    raw["entries"][0]["bam_relpath"] = "practice/round_alt/input.bam"
    raw = _rehash(raw)
    result = verify_inventory(raw, synthetic_manifest())
    assert result.embedded_matches is True  # recomputed to match the tampered content
    assert result.checks["inventory_paths_identity_bound"] is False
    assert result.ok is False


def test_accepted_inventory_paths_identity_bound_true():
    result = verify_inventory(_inv_dict(), synthetic_manifest())
    assert result.checks["inventory_paths_identity_bound"] is True


def test_derived_paths_match_all_entries():
    from minos_engine.qualification.layer2_split_runner import derive_inventory_paths

    manifest = synthetic_manifest()
    by_id = {s.dataset_id: (s.round_id, s.chromosome) for s in manifest.samples}
    for e in synthetic_inventory().entries:
        rid, chrom = by_id[e.dataset_id]
        expected = derive_inventory_paths(rid, chrom)
        assert e.bam_relpath == expected["bam_relpath"]
        assert e.bai_relpath == expected["bai_relpath"]
        assert e.reference_relpath == expected["reference_relpath"]
        assert e.fai_relpath == expected["fai_relpath"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("bam_relpath", "practice/round_alt/input.bam"),
        ("bai_relpath", "practice/round_alt/input.bam.bai"),
        ("reference_relpath", "reference/chrOther/chrOther.fa"),
        ("fai_relpath", "reference/chrOther/chrOther.fa.fai"),
    ],
)
def test_each_path_field_identity_substitution_rejected(field, value):
    raw = _inv_dict()
    raw["entries"][0][field] = value
    raw = _rehash(raw)  # consistently re-hashed, still syntactically safe
    result = verify_inventory(raw, synthetic_manifest())
    assert result.ok is False
    assert result.checks["inventory_paths_identity_bound"] is False
    # correspondence + safe-path still pass; only the derived-path identity fails.
    assert result.checks["inventory_manifest_identity_bound"] is True
    assert result.checks["inventory_paths_safe"] is True


# --------------------------------------------------------------------------- #
# Frozen manifest correspondence (the independent anchor)
# --------------------------------------------------------------------------- #
def test_entry_removed_rejected():
    raw = _inv_dict()
    raw["entries"].pop()
    raw = _rehash(raw)  # even a fully consistent 74-entry inventory must fail
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks["inventory_manifest_identity_bound"] is False


def test_duplicate_entry_rejected():
    raw = _inv_dict()
    raw["entries"].append(copy.deepcopy(raw["entries"][0]))
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok  # contract rejects duplicate dataset_id
    assert result.checks.get("inventory_contract_valid") is False


def test_dataset_id_mismatch_rejected():
    raw = _inv_dict()
    raw["entries"][0]["dataset_id"] = "minos-chrXX-deadbeefdeadbeef"
    raw = _rehash(raw)  # consistent hash, but the id set no longer matches the manifest
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks["inventory_manifest_identity_bound"] is False


def test_round_id_mismatch_rejected():
    raw = _inv_dict()
    raw["entries"][0]["round_id"] = "ffffffffffffffff"
    raw = _rehash(raw)
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks["inventory_manifest_identity_bound"] is False


def test_chromosome_mismatch_rejected():
    raw = _inv_dict()
    # move the entry to a different (still-supported) chromosome; id set diverges.
    raw["entries"][0]["chromosome"] = "chr22"
    raw = _rehash(raw)
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks["inventory_manifest_identity_bound"] is False


# --------------------------------------------------------------------------- #
# Safe-path policy (independent of the frozen contract)
# --------------------------------------------------------------------------- #
def test_absolute_path_rejected():
    raw = _inv_dict()
    raw["entries"][0]["bam_relpath"] = "/etc/passwd"
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok  # the contract itself forbids absolute paths
    assert result.checks.get("inventory_contract_valid") is False


def test_windows_drive_path_rejected():
    raw = _inv_dict()
    raw["entries"][0]["bam_relpath"] = "C:/data/input.bam"
    raw = _rehash(raw)  # passes the contract, must fail the safe-path policy
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks["inventory_paths_safe"] is False


def test_backslash_path_rejected():
    raw = _inv_dict()
    raw["entries"][0]["bam_relpath"] = "practice\\round\\input.bam"
    raw = _rehash(raw)
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks["inventory_paths_safe"] is False


def test_traversal_path_rejected():
    raw = _inv_dict()
    raw["entries"][0]["bam_relpath"] = "../../etc/input.bam"
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok  # the contract forbids '..' segments
    assert result.checks.get("inventory_contract_valid") is False


def test_uri_path_rejected():
    raw = _inv_dict()
    raw["entries"][0]["bam_relpath"] = "file://host/input.bam"
    raw = _rehash(raw)
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks["inventory_paths_safe"] is False


# --------------------------------------------------------------------------- #
# Unknown fields + leakage isolation + malformed structure
# --------------------------------------------------------------------------- #
def test_unknown_field_rejected():
    raw = _inv_dict()
    raw["entries"][0]["secret_truth_path"] = "x"
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok  # extra="forbid"
    assert result.checks.get("inventory_contract_valid") is False


def test_truth_mutation_path_rejected():
    raw = _inv_dict()
    raw["entries"][0]["reference_relpath"] = "reference/truth/chr18.fa"
    raw = _rehash(raw)
    result = verify_inventory(raw, synthetic_manifest())
    assert not result.ok
    assert result.checks["inventory_truth_mutation_isolation_ok"] is False


def test_malformed_inventory_object_rejected():
    result = verify_inventory({"entries": "not-a-list"}, synthetic_manifest())
    assert not result.ok
    assert result.checks == {"inventory_contract_valid": False}


# --------------------------------------------------------------------------- #
# Safe-path helper direct cases
# --------------------------------------------------------------------------- #
def test_inv_path_safe_matrix():
    assert _inv_path_safe("practice/round_x/input.bam") is True
    assert _inv_path_safe("reference/chr18/chr18.fa.fai") is True
    for bad in (
        "",
        "/abs/path",
        "a\\b",
        "C:/x",
        "c:\\x",
        "file://h/x",
        "s3://bucket/x",
        "../x",
        "a/../b",
        "a/./b",
        "a//b",
        "trailing/",
    ):
        assert _inv_path_safe(bad) is False, bad


# --------------------------------------------------------------------------- #
# Unique evidence resolution
# --------------------------------------------------------------------------- #
def _item(path: str, kind: EvidenceKind, sha: str | None = _H) -> EvidenceItem:
    return EvidenceItem(description="d", path=path, kind=kind, sha256=sha)


def test_unique_evidence_ok():
    ev = (_item("p", EvidenceKind.DIRECTORY),)
    assert _unique_evidence(ev, "p", EvidenceKind.DIRECTORY) is not None


def test_unique_evidence_missing():
    assert _unique_evidence((), "p", EvidenceKind.DIRECTORY) is None


def test_unique_evidence_duplicate():
    ev = (_item("p", EvidenceKind.DIRECTORY), _item("p", EvidenceKind.DIRECTORY))
    assert _unique_evidence(ev, "p", EvidenceKind.DIRECTORY) is None


def test_unique_evidence_wrong_kind():
    ev = (_item("p", EvidenceKind.FILE),)
    assert _unique_evidence(ev, "p", EvidenceKind.DIRECTORY) is None


def test_unique_evidence_missing_hash():
    # EvidenceItem forbids a malformed sha at construction, so the reachable failure is
    # a null sha256 — which must also fail to resolve a unique bindable item.
    ev = (_item("p", EvidenceKind.DIRECTORY, sha=None),)
    assert _unique_evidence(ev, "p", EvidenceKind.DIRECTORY) is None


# --------------------------------------------------------------------------- #
# Evidence payload aggregate: deterministic, order-independent, content-sensitive
# --------------------------------------------------------------------------- #
def test_evidence_payload_hash_order_independent():
    a = [("m.json", "file", "1" * 64), ("i.json", "file", "2" * 64), ("r.md", "file", "3" * 64)]
    b = list(reversed(a))
    assert evidence_payload_hash(a) == evidence_payload_hash(b)


def test_evidence_payload_hash_content_sensitive():
    a = [("m.json", "file", "1" * 64), ("i.json", "file", "2" * 64), ("r.md", "file", "3" * 64)]
    c = [("m.json", "file", "1" * 64), ("i.json", "file", "2" * 64), ("r.md", "file", "9" * 64)]
    assert evidence_payload_hash(a) != evidence_payload_hash(c)
