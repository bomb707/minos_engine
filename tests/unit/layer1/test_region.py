"""Group C (region) — coordinate resolution known-answer + boundary tests."""

from __future__ import annotations

import pytest

from minos_engine.layer1.region import RegionResolutionError, resolve_region

BAM = {"chr1": 1000, "chr2": 500}
FA = {"chr1": 1000, "chr2": 500}


def test_one_based_inclusive_conversion():
    r = resolve_region("chr1:11-20", "one_based_inclusive", BAM, FA)
    assert (r.start0, r.end0_exclusive, r.length_bp) == (10, 20, 10)
    assert r.verified is True


def test_zero_based_half_open_passthrough():
    r = resolve_region("chr1:10-20", "zero_based_half_open", BAM, FA)
    assert (r.start0, r.end0_exclusive) == (10, 20)


def test_one_base_interval():
    r = resolve_region("chr1:5-5", "one_based_inclusive", BAM, FA)
    assert (r.start0, r.end0_exclusive, r.length_bp) == (4, 5, 1)


def test_contig_end_boundary_ok():
    r = resolve_region("chr1:991-1000", "one_based_inclusive", BAM, FA)
    assert r.end0_exclusive == 1000


def test_region_beyond_contig_rejected():
    with pytest.raises(RegionResolutionError):
        resolve_region("chr1:995-1005", "one_based_inclusive", BAM, FA)


def test_unknown_convention_rejected():
    with pytest.raises(RegionResolutionError):
        resolve_region("chr1:1-10", "made_up", BAM, FA)


def test_abbreviation_rejected():
    with pytest.raises(RegionResolutionError):
        resolve_region("chr1:1-10k", "one_based_inclusive", BAM, FA)


def test_missing_contig_in_bam_rejected():
    with pytest.raises(RegionResolutionError):
        resolve_region("chrX:1-10", "one_based_inclusive", BAM, FA)


def test_length_mismatch_between_bam_and_fasta_rejected():
    with pytest.raises(RegionResolutionError):
        resolve_region("chr1:1-10", "one_based_inclusive", {"chr1": 1000}, {"chr1": 999})


def test_unparseable_rejected():
    with pytest.raises(RegionResolutionError):
        resolve_region("chr1-1-10", "one_based_inclusive", BAM, FA)


def test_roundtrip_property():
    for start1 in (1, 3, 250, 991):
        r = resolve_region(f"chr1:{start1}-{start1 + 4}", "one_based_inclusive", BAM, FA)
        # zero-based half-open recovered back to one-based inclusive
        assert r.start0 + 1 == start1
        assert r.end0_exclusive == start1 + 4
