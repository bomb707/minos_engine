"""Unit tests for the L2-C manifest generator and non-mutating verifier."""

from __future__ import annotations

from minos_engine.layer2.split.generator import build_manifest
from minos_engine.layer2.split.verifier import verify_manifest
from tests.layer2c_synth import synthetic_raw_samples


def test_generate_exact_counts_and_disjoint_union():
    m = build_manifest(synthetic_raw_samples())
    assert m.counts == {"train": 50, "validation": 10, "test": 15}
    buckets: dict[str, set[str]] = {"train": set(), "validation": set(), "test": set()}
    for s in m.samples:
        buckets[s.partition].add(s.dataset_id)
    all_ids = {s.dataset_id for s in m.samples}
    assert len(all_ids) == 75
    assert buckets["train"] | buckets["validation"] | buckets["test"] == all_ids
    assert not (buckets["train"] & buckets["validation"])
    assert not (buckets["train"] & buckets["test"])
    assert not (buckets["validation"] & buckets["test"])
    for c in ("chr18", "chr19", "chr20", "chr21", "chr22"):
        assert m.per_chromosome[c] == {"train": 10, "validation": 2, "test": 3}


def test_generate_is_byte_identical_regardless_of_input_order():
    a = build_manifest(synthetic_raw_samples())
    shuffled = list(reversed(synthetic_raw_samples()))
    b = build_manifest(shuffled)
    assert a.to_canonical() == b.to_canonical()
    assert a.manifest_hash == b.manifest_hash


def test_verify_passes_on_generated_manifest():
    m = build_manifest(synthetic_raw_samples())
    v = verify_manifest(m.to_canonical())
    assert v.ok, v.reasons


def test_verify_rejects_moved_partition_inconsistent_hash():
    m = build_manifest(synthetic_raw_samples())
    raw = m.to_canonical()
    # flip one train sample to test WITHOUT recomputing hashes -> manifest_hash mismatch
    # is caught by the contract itself (fails closed early).
    for s in raw["samples"]:
        if s["partition"] == "train":
            s["partition"] = "test"
            break
    v = verify_manifest(raw)
    assert not v.ok
    assert v.checks["contract_valid"] is False


def test_verify_rejects_reassignment_with_consistent_hash():
    from minos_engine.layer2.split.contracts import DatasetSplitManifest

    m = build_manifest(synthetic_raw_samples())
    raw = m.to_canonical()
    # swap partitions of a train and a test sample in the SAME chromosome, keeping counts,
    # then recompute the manifest hash so the contract accepts it -> only the independent
    # policy re-derivation catches the reassignment.
    chrom = raw["samples"][0]["chromosome"]
    tr = next(s for s in raw["samples"] if s["chromosome"] == chrom and s["partition"] == "train")
    te = next(s for s in raw["samples"] if s["chromosome"] == chrom and s["partition"] == "test")
    tr["partition"], te["partition"] = "test", "train"
    raw["manifest_hash"] = ""
    raw["dataset_registry_hash"] = ""
    rebuilt = DatasetSplitManifest.model_validate(raw).to_canonical()
    v = verify_manifest(rebuilt)
    assert not v.ok
    assert v.checks["assignment_matches_policy"] is False


def test_verify_rejects_wrong_totals():
    m = build_manifest(synthetic_raw_samples())
    raw = m.to_canonical()
    raw["counts"] = {"train": 49, "validation": 11, "test": 15}
    v = verify_manifest(raw)
    assert not v.ok


def test_verify_rejects_dropped_sample():
    m = build_manifest(synthetic_raw_samples())
    raw = m.to_canonical()
    raw["samples"] = raw["samples"][:-1]
    v = verify_manifest(raw)
    assert not v.ok  # inconsistent manifest_hash -> contract fails closed
    assert v.checks["contract_valid"] is False


def test_verify_rejects_unknown_field():
    m = build_manifest(synthetic_raw_samples())
    raw = m.to_canonical()
    raw["samples"][0]["leak"] = "x"
    v = verify_manifest(raw)
    assert not v.ok
    assert not v.checks["schema_valid"]
