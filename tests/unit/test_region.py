"""Region representation and exact one-time coordinate conversion."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.intake.contracts import Region


def test_one_based_inclusive_conversion():
    r = Region.from_source("chr19:13000000-23000000", "one_based_inclusive")
    assert r.start0 == 12999999
    assert r.end0_exclusive == 23000000
    assert r.length_bp == 10000001


def test_zero_based_half_open_passthrough():
    r = Region.from_source("chr1:0-100", "zero_based_half_open")
    assert (r.start0, r.end0_exclusive, r.length_bp) == (0, 100, 100)


def test_one_base_interval():
    r = Region.from_source("chr7:5-5", "one_based_inclusive")
    assert (r.start0, r.end0_exclusive, r.length_bp) == (4, 5, 1)


def test_contig_start_boundary():
    r = Region.from_source("chrX:1-2", "one_based_inclusive")
    assert r.start0 == 0


def test_unknown_convention_rejected():
    with pytest.raises(ValueError):
        Region.from_source("chr1:1-2", "banana")


def test_malformed_region_rejected():
    for bad in ["chr1:10-5", "chr1:1_000-2_000", "1:1-2", "chrZ:1-2", "chr1:10Mb-20Mb"]:
        with pytest.raises((ValueError, ValidationError)):
            Region.from_source(bad, "one_based_inclusive")


def test_inverted_interval_rejected():
    # one-based inclusive chr1:10-5 -> start0=9, end0=5 -> invalid
    with pytest.raises((ValueError, ValidationError)):
        Region.from_source("chr1:10-5", "one_based_inclusive")


def test_length_bp_must_match():
    with pytest.raises(ValidationError):
        Region(
            source="chr1:1-2",
            source_coordinate_system="one_based_inclusive",
            contig="chr1",
            start0=0,
            end0_exclusive=2,
            length_bp=999,
            verified=True,
        )
