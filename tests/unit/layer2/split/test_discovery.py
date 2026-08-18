"""Fail-closed discovery tests using header-only BAMs (no real corpus)."""

from __future__ import annotations

from pathlib import Path

import pysam
import pytest

from minos_engine.layer2.split.discovery import DiscoveryError, discover_corpus
from minos_engine.layer2.split.policy import SUPPORTED_CHROMOSOMES

_LEN = {
    "chr18": 80373285,
    "chr19": 58617616,
    "chr20": 64444167,
    "chr21": 46709983,
    "chr22": 50818468,
}


def _write_bam(path: Path, contig: str, length: int, rid: str = "x") -> None:
    # A per-round @CO comment makes each header-only BAM's bytes unique (distinct
    # identity tuples), mirroring the distinct real practice BAMs.
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": contig, "LN": length}], "CO": [f"round:{rid}"]}
    with pysam.AlignmentFile(str(path), "wb", header=header):
        pass


def _make_round(root: Path, chrom: str, rid: str, *, length: int | None = None, bai: bool = True):
    length = _LEN[chrom] if length is None else length
    rd = root / "practice" / f"round_{rid}"
    rd.mkdir(parents=True)
    _write_bam(rd / "input.bam", chrom, length, rid)
    if bai:
        (rd / "input.bam.bai").write_bytes(f"idx-{rid}".encode())
    refdir = root / "reference" / chrom
    refdir.mkdir(parents=True, exist_ok=True)
    (refdir / f"{chrom}.fa").write_bytes(b">%b\nACGT\n" % chrom.encode())
    (refdir / f"{chrom}.fa.fai").write_text(f"{chrom}\t{_LEN[chrom]}\t6\t60\t61\n")


def _full_corpus(root: Path) -> None:
    for ci, chrom in enumerate(SUPPORTED_CHROMOSOMES):
        for i in range(15):
            _make_round(root, chrom, f"{ci:x}{i:015x}")


def test_full_synthetic_corpus_discovers_75(tmp_path):
    _full_corpus(tmp_path)
    samples = discover_corpus(tmp_path)
    assert len(samples) == 75
    by = {}
    for s in samples:
        by[s.chromosome] = by.get(s.chromosome, 0) + 1
    assert by == dict.fromkeys(SUPPORTED_CHROMOSOMES, 15)


def test_missing_bai_rejected(tmp_path):
    _make_round(tmp_path, "chr18", "0" * 16, bai=False)
    with pytest.raises(DiscoveryError, match="missing required input"):
        discover_corpus(tmp_path)


def test_empty_bam_rejected(tmp_path):
    _make_round(tmp_path, "chr18", "0" * 16)
    (tmp_path / "practice" / f"round_{'0' * 16}" / "input.bam").write_bytes(b"")
    with pytest.raises(DiscoveryError, match="BAM"):
        discover_corpus(tmp_path)


def test_empty_bai_rejected(tmp_path):
    _make_round(tmp_path, "chr18", "0" * 16)
    (tmp_path / "practice" / f"round_{'0' * 16}" / "input.bam.bai").write_bytes(b"")
    with pytest.raises(DiscoveryError, match="empty file"):
        discover_corpus(tmp_path)


def test_sq_fai_length_mismatch_rejected(tmp_path):
    _make_round(tmp_path, "chr18", "0" * 16, length=123)  # @SQ length != FAI length
    with pytest.raises(DiscoveryError, match="length"):
        discover_corpus(tmp_path)


def test_unsupported_chromosome_rejected(tmp_path):
    rd = tmp_path / "practice" / "round_aaaa"
    rd.mkdir(parents=True)
    _write_bam(rd / "input.bam", "chrX", 1000)
    (rd / "input.bam.bai").write_bytes(b"idx")
    (tmp_path / "reference").mkdir()
    with pytest.raises(DiscoveryError, match="unsupported chromosome"):
        discover_corpus(tmp_path)


def test_wrong_per_chromosome_count_rejected(tmp_path):
    _make_round(tmp_path, "chr18", "0" * 16)  # only 1 sample total
    with pytest.raises(DiscoveryError):
        discover_corpus(tmp_path)


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / "outside_bam"
    _write_bam(outside, "chr18", _LEN["chr18"])
    rd = tmp_path / "practice" / f"round_{'0' * 16}"
    rd.mkdir(parents=True)
    (rd / "input.bam").symlink_to(outside)
    (rd / "input.bam.bai").write_bytes(b"idx")
    refdir = tmp_path / "reference" / "chr18"
    refdir.mkdir(parents=True)
    (refdir / "chr18.fa").write_bytes(b">chr18\nACGT\n")
    (refdir / "chr18.fa.fai").write_text(f"chr18\t{_LEN['chr18']}\t6\t60\t61\n")
    with pytest.raises(DiscoveryError, match="escapes dataset root"):
        discover_corpus(tmp_path)
