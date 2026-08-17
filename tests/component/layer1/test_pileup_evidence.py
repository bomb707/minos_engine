"""Group I — bounded pileup and truth-free evidence proxies (known-answer)."""

from __future__ import annotations

from pathlib import Path

import pysam
from tests.layer1_fixtures import ReadSpec, write_bam, write_reference

from minos_engine.layer1.pileup import aggregate_evidence, profile_pileup_window

SUPPORT = (2, 3, 5, 8, 10)
AFS = (0.05, 0.10, 0.20, 0.30, 0.40)


def _alt_reads(n: int, start: int, length: int) -> list[ReadSpec]:
    seq = "C" * length
    quals = [35] * length
    reads = []
    for i in range(n):
        reads.append(
            ReadSpec(
                f"r{i}",
                start,
                [(0, length)],
                seq,
                quals,
                is_paired=False,
                is_reverse=(i % 2 == 1),
                nm=length,
            )
        )
    return reads


def test_full_mismatch_generates_candidate_snps(tmp_path: Path):
    length = 20
    ref = "A" * 200
    write_reference(tmp_path / "chr1.fa", "chr1", ref)
    bam, _ = write_bam(tmp_path / "input.bam", "chr1", 200, _alt_reads(10, 40, length))
    af = pysam.AlignmentFile(str(bam), "rb")
    fa = pysam.FastaFile(str(tmp_path / "chr1.fa"))
    ev = profile_pileup_window(
        af,
        fa,
        "chr1",
        40,
        60,
        max_depth=1000,
        stepper="samtools",
        support_thresholds=SUPPORT,
        af_thresholds=AFS,
    )
    af.close()
    fa.close()
    assert ev.columns == 20
    assert ev.callable_columns == 20
    assert ev.snp_sites == 20  # every column is a full-support mismatch
    assert ev.alt_base_obs == 200  # 10 reads x 20 columns
    assert ev.forward_alt == 100 and ev.reverse_alt == 100
    assert ev.insertion_sites == 0 and ev.deletion_sites == 0


def test_reference_matching_reads_have_no_candidates(tmp_path: Path):
    length = 20
    write_reference(tmp_path / "chr1.fa", "chr1", "C" * 200)  # ref matches reads
    bam, _ = write_bam(tmp_path / "input.bam", "chr1", 200, _alt_reads(10, 40, length))
    af = pysam.AlignmentFile(str(bam), "rb")
    fa = pysam.FastaFile(str(tmp_path / "chr1.fa"))
    ev = profile_pileup_window(
        af,
        fa,
        "chr1",
        40,
        60,
        max_depth=1000,
        stepper="samtools",
        support_thresholds=SUPPORT,
        af_thresholds=AFS,
    )
    af.close()
    fa.close()
    assert ev.snp_sites == 0
    assert ev.alt_base_obs == 0


def test_aggregate_evidence_densities(tmp_path: Path):
    length = 20
    write_reference(tmp_path / "chr1.fa", "chr1", "A" * 200)
    bam, _ = write_bam(tmp_path / "input.bam", "chr1", 200, _alt_reads(10, 40, length))
    af = pysam.AlignmentFile(str(bam), "rb")
    fa = pysam.FastaFile(str(tmp_path / "chr1.fa"))
    ev = profile_pileup_window(
        af,
        fa,
        "chr1",
        40,
        60,
        max_depth=1000,
        stepper="samtools",
        support_thresholds=SUPPORT,
        af_thresholds=AFS,
    )
    af.close()
    fa.close()
    agg = aggregate_evidence([ev], eligible_region_bases=200, analyzed_bases=20)
    assert agg.mismatch_fraction == 1.0
    assert agg.candidate_snp_density_per_base == 20 / 20
    assert agg.columns_reaching_max_depth == 0
    assert "support_ge_2" in agg.support_threshold_site_counts
