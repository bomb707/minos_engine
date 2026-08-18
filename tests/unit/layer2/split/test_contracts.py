"""Unit tests for L2-C contracts, canonical serialization, and manifest hashing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.layer2.split.contracts import (
    DatasetSplitManifest,
    SampleIdentity,
    region_hash_for,
)
from tests.layer2c_synth import synthetic_manifest, synthetic_raw_samples


def test_manifest_hash_excludes_nothing_volatile_and_is_stable():
    m1 = synthetic_manifest()
    m2 = synthetic_manifest()
    assert m1.manifest_hash == m2.manifest_hash
    assert m1.to_canonical() == m2.to_canonical()


def test_manifest_hash_recomputes():
    m = synthetic_manifest()
    assert m.manifest_hash == m.compute_manifest_hash()
    assert m.dataset_registry_hash != ""


def test_manifest_rejects_unknown_field():
    m = synthetic_manifest()
    raw = m.to_canonical()
    raw["unexpected"] = 1
    with pytest.raises(ValidationError):
        DatasetSplitManifest.model_validate(raw)


def test_manifest_rejects_tampered_manifest_hash():
    m = synthetic_manifest()
    raw = m.to_canonical()
    raw["manifest_hash"] = "0" * 64
    with pytest.raises(ValidationError):
        DatasetSplitManifest.model_validate(raw)


def test_manifest_rejects_tampered_registry_hash():
    m = synthetic_manifest()
    raw = m.to_canonical()
    raw["dataset_registry_hash"] = "0" * 64
    raw["manifest_hash"] = ""  # allow manifest hash to recompute
    with pytest.raises(ValidationError):
        DatasetSplitManifest.model_validate(raw)


def test_no_paths_or_truth_in_canonical():
    blob = str(synthetic_manifest().to_canonical()).lower()
    for token in ("/input.bam", "practice/", "truth", "mutation", ".vcf", "reference/"):
        assert token not in blob


def test_sample_region_hash_mismatch_rejected():
    raw = synthetic_raw_samples()[0]
    with pytest.raises(ValidationError):
        SampleIdentity(
            dataset_id="minos-chr18-x",
            round_id=raw.round_id,
            chromosome="chr18",
            region_source="chr18:0-100",
            region_contig="chr18",
            region_start0=0,
            region_end0_exclusive=100,
            region_length_bp=100,
            region_hash="0" * 64,  # wrong
            bam_sha256=raw.bam_sha256,
            bai_sha256=raw.bai_sha256,
            reference_sha256=raw.reference_sha256,
            fai_sha256=raw.fai_sha256,
            bam_size_bytes=1,
            parameter_space_hash="1" * 64,
            feature_registry_hash="2" * 64,
            split_algorithm_version="layer2-dataset-split-v1",
            split_salt="minos-l2-split-v1",
            allocation_digest="3" * 64,
            partition="train",
            sort_order=0,
        )


def test_sample_bad_partition_rejected():
    with pytest.raises(ValidationError):
        SampleIdentity(
            dataset_id="d",
            round_id="a",
            chromosome="chr18",
            region_source="chr18:0-100",
            region_contig="chr18",
            region_start0=0,
            region_end0_exclusive=100,
            region_length_bp=100,
            region_hash=region_hash_for("chr18", 0, 100),
            bam_sha256="a" * 64,
            bai_sha256="b" * 64,
            reference_sha256="c" * 64,
            fai_sha256="d" * 64,
            bam_size_bytes=1,
            parameter_space_hash="1" * 64,
            feature_registry_hash="2" * 64,
            split_algorithm_version="v",
            split_salt="s",
            allocation_digest="3" * 64,
            partition="holdout",  # invalid
            sort_order=0,
        )


def test_identity_tuple_binds_inputs():
    m = synthetic_manifest()
    s = m.samples[0]
    # identity tuple hash is derived from the (bam,bai,ref,fai,region) tuple
    assert len(s.identity_tuple_hash) == 64
