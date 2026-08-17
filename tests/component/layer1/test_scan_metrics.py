"""Group E — one-pass scan known-answer metrics (independently computed)."""

from __future__ import annotations

from pathlib import Path

import pysam
import pytest
from tests.layer1_fixtures import ReadSpec, write_bam, write_reference

from minos_engine.layer1.filters import ReadFilterPolicy
from minos_engine.layer1.scan import OnePassScanner

SEQ10 = "ACGTACGTAC"
Q10 = [35] * 10


def _controlled_reads() -> list[ReadSpec]:
    reads = [
        # two clean proper pairs (4 eligible, paired)
        ReadSpec("p0", 100, [(0, 10)], SEQ10, Q10, is_read1=True, mate_start0=150, tlen=60),
        ReadSpec(
            "p0",
            150,
            [(0, 10)],
            SEQ10,
            Q10,
            is_read1=False,
            is_reverse=True,
            mate_start0=100,
            tlen=-60,
        ),
        ReadSpec("p1", 110, [(0, 10)], SEQ10, Q10, is_read1=True, mate_start0=160, tlen=60),
        ReadSpec(
            "p1",
            160,
            [(0, 10)],
            SEQ10,
            Q10,
            is_read1=False,
            is_reverse=True,
            mate_start0=110,
            tlen=-60,
        ),
        # excluded reads (one per reason)
        ReadSpec("dup", 100, [(0, 10)], SEQ10, Q10, is_paired=False, is_dup=True),
        ReadSpec("sec", 100, [(0, 10)], SEQ10, Q10, is_paired=False, is_secondary=True),
        ReadSpec("sup", 100, [(0, 10)], SEQ10, Q10, is_paired=False, is_supplementary=True),
        ReadSpec("qc", 100, [(0, 10)], SEQ10, Q10, is_paired=False, is_qcfail=True),
        # eligible feature reads
        ReadSpec("mq0", 120, [(0, 10)], SEQ10, Q10, is_paired=False, mapq=0),
        ReadSpec("sc", 130, [(4, 2), (0, 8)], SEQ10, Q10, is_paired=False),  # 2S8M
        ReadSpec("ins", 140, [(0, 4), (1, 2), (0, 4)], SEQ10, Q10, is_paired=False),  # 4M2I4M
        ReadSpec("del", 150, [(0, 4), (2, 2), (0, 6)], SEQ10, Q10, is_paired=False),  # 4M2D6M
        ReadSpec("noq", 160, [(0, 10)], SEQ10, None, is_paired=False),  # missing quals
    ]
    return reads


@pytest.fixture
def scanner(tmp_path: Path) -> OnePassScanner:
    write_reference(tmp_path / "chr1.fa", "chr1", "ACGT" * 125)
    bam, _ = write_bam(tmp_path / "input.bam", "chr1", 500, _controlled_reads())
    af = pysam.AlignmentFile(str(bam), "rb")
    sc = OnePassScanner(0, 500, 100000, ReadFilterPolicy())
    for r in af.fetch("chr1", 0, 500):
        sc.observe(r)
    af.close()
    return sc


def test_observed_and_filter_accounting(scanner: OnePassScanner):
    fc = scanner.filter_counts()
    assert fc.observed == 13
    assert fc.included == 9
    assert fc.excluded_duplicate == 1
    assert fc.excluded_secondary == 1
    assert fc.excluded_supplementary == 1
    assert fc.excluded_qcfail == 1
    # accounting is mutually exclusive and complete
    assert fc.included + 4 == fc.observed


def test_alignment_fractions(scanner: OnePassScanner):
    am = scanner.alignment_metrics()
    assert am.total_observed_alignments == 13
    assert am.duplicate_fraction == 1 / 13
    assert am.secondary_fraction == 1 / 13


def test_mapping_quality_mq0(scanner: OnePassScanner):
    mq = scanner.mapping_quality_metrics()
    assert mq.count == 9
    assert mq.mq0_fraction == 1 / 9  # only the mq0 read


def test_cigar_clip_and_indel(scanner: OnePassScanner):
    cg = scanner.cigar_metrics()
    assert cg.soft_clipped_bases == 2
    assert cg.soft_clipped_read_fraction == 1 / 9
    # query-consuming bases: 9 eligible reads x 10 = 90
    assert cg.query_consuming_bases == 90
    assert cg.soft_clipped_base_fraction == 2 / 90
    assert cg.indel_bearing_read_fraction == 2 / 9  # ins + del reads
    assert cg.inserted_bases == 2
    assert cg.deleted_bases == 2


def test_base_quality_missing_tracked(scanner: OnePassScanner):
    bq = scanner.base_quality_metrics()
    # 8 eligible reads have 10 quals each = 80; the 'noq' read has none
    assert bq.bases_with_quality == 80
    assert bq.bases_observed == 90
    assert 0.0 < bq.missing_quality_fraction < 0.2


def test_pairing_completed_pairs(scanner: OnePassScanner):
    fm = scanner.fragment_metrics()
    assert fm.eligible_pair_count == 2  # two completed proper pairs
