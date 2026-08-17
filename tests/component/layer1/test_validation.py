"""Group B — BAM/BAI/reference integrity validation with real tiny fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.layer1_fixtures import build_dataset, simple_reads

from minos_engine.layer1.adapters.pysam_adapter import PysamAdapter
from minos_engine.layer1.validation import Layer1InputError, validate_inputs

ADAPTER = PysamAdapter()


def _validate(ds, region=None):
    inputs = validate_inputs(
        bam_path=str(ds.bam),
        bai_path=str(ds.bai),
        reference_path=str(ds.reference),
        fai_path=str(ds.fai),
        region_source=region or ds.region_source,
        region_convention="one_based_inclusive",
        adapter=ADAPTER,
    )
    inputs.alignment.close()
    inputs.fasta.close()
    return inputs


def test_valid_inputs(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(2000, n_pairs=20), contig_len=2000)
    inputs = _validate(ds, region="chr1:1-2000")
    assert inputs.region.contig == "chr1"
    assert inputs.header.coordinate_sorted
    assert inputs.identity.verification_strength.value == "content_hash_and_fetch"
    assert len(inputs.identity.bam_sha256) == 64
    assert inputs.header.sample_names == ("synthetic",)


def test_missing_bam(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(2000, n_pairs=5), contig_len=2000)
    ds.bam.unlink()
    with pytest.raises(Layer1InputError, match="BAM not found"):
        _validate(ds)


def test_missing_index(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(2000, n_pairs=5), contig_len=2000, index=False)
    with pytest.raises(Layer1InputError):
        _validate(ds)


def test_corrupt_index(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(2000, n_pairs=5), contig_len=2000, corrupt_index=True)
    with pytest.raises(Layer1InputError):
        _validate(ds)


def test_unsorted_bam_rejected(tmp_path: Path):
    ds = build_dataset(
        tmp_path,
        simple_reads(2000, n_pairs=5),
        contig_len=2000,
        sort_order="queryname",
        index=True,
    )
    with pytest.raises(Layer1InputError, match="coordinate-sorted"):
        _validate(ds)


def test_region_out_of_contig(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(2000, n_pairs=5), contig_len=2000)
    with pytest.raises(Layer1InputError):
        _validate(ds, region="chr1:1-5000")


def test_contig_length_mismatch(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(2000, n_pairs=5), contig_len=2000)
    # rebuild a reference of a different length under the same contig name
    from tests.layer1_fixtures import write_reference

    write_reference(ds.reference, "chr1", "A" * 1999)
    with pytest.raises(Layer1InputError):
        _validate(ds, region="chr1:1-1999")


def test_coordinate_conversion_recorded(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(2000, n_pairs=5), contig_len=2000)
    inputs = _validate(ds, region="chr1:11-20")
    assert inputs.region.start0 == 10
    assert inputs.region.end0_exclusive == 20
    assert inputs.region.source_coordinate_system == "one_based_inclusive"
