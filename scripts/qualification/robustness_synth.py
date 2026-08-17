"""Synthetic robustness fixtures for conditions the practice corpus lacks.

The corpus has zero marked duplicates / secondary / supplementary / qcfail reads,
so those handling paths (and several error paths) are validated here on small
deterministic pysam fixtures — explicitly synthetic, never claimed as real-corpus
validation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from tests.layer1_fixtures import ReadSpec, build_dataset, simple_reads, write_reference

from minos_engine.common.errors import ContractValidationError, MinosEngineError
from minos_engine.layer1.adapters.pysam_adapter import PysamAdapter
from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import ProfileRequest
from minos_engine.layer1.service import Layer1Service
from minos_engine.layer1.validation import Layer1InputError, validate_inputs

SEQ10 = "ACGTACGTAC"
Q10 = [35] * 10


def _profile(ds, region="chr1:1-3000"):  # type: ignore[no-untyped-def]
    cfg = load_layer1_config()
    req = ProfileRequest(
        round_id="rob",
        bam_path=str(ds.bam),
        bai_path=str(ds.bai),
        reference_path=str(ds.reference),
        fai_path=str(ds.fai),
        region_source=region,
        region_coordinate_convention="one_based_inclusive",
        budget_seconds=120,
        cpu_limit=1,
        memory_limit_bytes=1_000_000_000,
        profiler_config_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
    )
    return Layer1Service(require_prerequisite=False).profile(req).profile


def run() -> dict[str, Any]:  # noqa: C901
    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # duplicates: 20 duplicate reads must be excluded and counted
        reads = simple_reads(3000, n_pairs=30)
        for i in range(20):
            reads.append(
                ReadSpec(f"dup{i}", 300, [(0, 10)], SEQ10, Q10, is_paired=False, is_dup=True)
            )
        ds = build_dataset(tmp / "dup", reads, contig="chr1", contig_len=3000)
        p = _profile(ds)
        out["duplicates"] = {
            "ok": p.filter_counts.excluded_duplicate == 20 and p.reads.duplicate_fraction > 0,
            "excluded_duplicate": p.filter_counts.excluded_duplicate,
        }

        # secondary + supplementary + qcfail
        reads = simple_reads(3000, n_pairs=30)
        for i in range(7):
            reads.append(
                ReadSpec(f"sec{i}", 400, [(0, 10)], SEQ10, Q10, is_paired=False, is_secondary=True)
            )
        for i in range(5):
            reads.append(
                ReadSpec(
                    f"sup{i}", 410, [(0, 10)], SEQ10, Q10, is_paired=False, is_supplementary=True
                )
            )
        for i in range(3):
            reads.append(
                ReadSpec(f"qc{i}", 420, [(0, 10)], SEQ10, Q10, is_paired=False, is_qcfail=True)
            )
        ds = build_dataset(tmp / "flags", reads, contig="chr1", contig_len=3000)
        p = _profile(ds)
        out["secondary_supplementary_qcfail"] = {
            "ok": p.filter_counts.excluded_secondary == 7
            and p.filter_counts.excluded_supplementary == 5
            and p.filter_counts.excluded_qcfail == 3,
            "counts": [
                p.filter_counts.excluded_secondary,
                p.filter_counts.excluded_supplementary,
                p.filter_counts.excluded_qcfail,
            ],
        }

        # overlapping mates: fragment_primary depth <= duplicate_including
        reads = []
        for i in range(50):
            s1 = 500 + i
            reads.append(
                ReadSpec(
                    f"ov{i}",
                    s1,
                    [(0, 30)],
                    "A" * 30,
                    [35] * 30,
                    is_read1=True,
                    mate_start0=s1 + 10,
                    tlen=40,
                )
            )
            reads.append(
                ReadSpec(
                    f"ov{i}",
                    s1 + 10,
                    [(0, 30)],
                    "A" * 30,
                    [35] * 30,
                    is_read1=False,
                    is_reverse=True,
                    mate_start0=s1,
                    tlen=-40,
                )
            )
        ds = build_dataset(tmp / "ov", reads, contig="chr1", contig_len=3000)
        p = _profile(ds)
        out["overlapping_mates"] = {
            "ok": p.coverage.fragment_primary.mean_depth_reads_per_base
            <= p.coverage.duplicate_including.mean_depth_reads_per_base + 1e-9
            and p.pairing.overlapping_mate_fraction > 0,
            "frag_mean": p.coverage.fragment_primary.mean_depth_reads_per_base,
            "dup_mean": p.coverage.duplicate_including.mean_depth_reads_per_base,
            "overlap_frac": p.pairing.overlapping_mate_fraction,
        }

        # missing base qualities
        reads = [
            ReadSpec(f"nq{i}", 600 + i, [(0, 10)], SEQ10, None, is_paired=False) for i in range(40)
        ]
        ds = build_dataset(tmp / "nq", reads, contig="chr1", contig_len=3000)
        p = _profile(ds)
        out["missing_qualities"] = {
            "ok": p.base_quality.missing_quality_fraction > 0.9
            and p.status.value in ("COMPLETE", "PARTIAL"),
            "missing_fraction": p.base_quality.missing_quality_fraction,
        }

        # unusual CIGAR: hard-clip + skip (N)
        reads = [
            ReadSpec(
                f"cg{i}",
                700 + i,
                [(5, 4), (0, 6), (3, 5), (0, 4)],
                "ACGTACGTAC",
                [30] * 10,
                is_paired=False,
            )
            for i in range(30)
        ]
        ds = build_dataset(tmp / "cg", reads, contig="chr1", contig_len=3000)
        p = _profile(ds)
        out["unusual_cigar"] = {
            "ok": p.alignment.hard_clipped_bases == 30 * 4 and p.alignment.skipped_bases == 30 * 5,
            "hard": p.alignment.hard_clipped_bases,
            "skip": p.alignment.skipped_bases,
        }

        # zero-depth region (region with no reads) -> no NaN, zero_depth ~ 1
        reads = simple_reads(3000, n_pairs=20, start=100)
        ds = build_dataset(tmp / "zero", reads, contig="chr1", contig_len=3000)
        p = _profile(ds, region="chr1:2500-2900")
        out["zero_depth_region"] = {
            "ok": p.coverage.duplicate_including.zero_depth_fraction == 1.0
            and p.status.value != "FAILED",
            "zero_frac": p.coverage.duplicate_including.zero_depth_fraction,
        }

        # high depth (deep pileup at one locus)
        reads = [
            ReadSpec(f"hd{i}", 800, [(0, 20)], "A" * 20, [35] * 20, is_paired=False)
            for i in range(500)
        ]
        ds = build_dataset(tmp / "hd", reads, contig="chr1", contig_len=3000)
        p = _profile(ds)
        out["high_depth"] = {
            "ok": p.coverage.duplicate_including.max_depth >= 500,
            "max_depth": p.coverage.duplicate_including.max_depth,
        }

        # zero MAPQ reads still processed
        reads = [
            ReadSpec(f"mq{i}", 900 + i, [(0, 10)], SEQ10, Q10, is_paired=False, mapq=0)
            for i in range(30)
        ]
        ds = build_dataset(tmp / "mq0", reads, contig="chr1", contig_len=3000)
        p = _profile(ds)
        out["zero_mapq"] = {
            "ok": p.mapping_quality.mq0_fraction == 1.0,
            "mq0": p.mapping_quality.mq0_fraction,
        }

        # malformed read group (no SM) -> warning, no crash
        ref = tmp / "rg" / "chr1.fa"
        (tmp / "rg").mkdir(parents=True)
        write_reference(ref, "chr1", "ACGT" * 750)
        import pysam

        hdr = pysam.AlignmentHeader.from_dict(
            {
                "HD": {"VN": "1.6", "SO": "coordinate"},
                "SQ": [{"SN": "chr1", "LN": 3000}],
                "RG": [{"ID": "1"}],
            }  # no SM
        )
        bam = tmp / "rg" / "input.bam"
        with pysam.AlignmentFile(str(bam), "wb", header=hdr) as o:
            for i in range(30):
                a = pysam.AlignedSegment(hdr)
                a.query_name = f"r{i}"
                a.flag = 0
                a.reference_id = 0
                a.reference_start = 100 + i
                a.mapping_quality = 60
                a.cigartuples = [(0, 10)]
                a.query_sequence = SEQ10
                a.query_qualities = pysam.qualitystring_to_array("I" * 10)
                o.write(a)
        pysam.index(str(bam))

        class _DS:
            pass

        ds2 = _DS()
        ds2.bam = bam
        ds2.bai = Path(str(bam) + ".bai")
        ds2.reference = ref
        ds2.fai = Path(str(ref) + ".fai")  # type: ignore[attr-defined]
        p = _profile(ds2)
        out["malformed_read_group"] = {
            "ok": any("SM" in w or "RG" in w for w in p.warnings) and p.status.value != "FAILED",
            "warnings": list(p.warnings),
        }

        # --- error/fail-closed cases ---
        ad = PysamAdapter()
        good = build_dataset(
            tmp / "good", simple_reads(3000, n_pairs=20), contig="chr1", contig_len=3000
        )

        def _expect_fail(name: str, **kw: str) -> None:
            try:
                vi = validate_inputs(adapter=ad, region_convention="one_based_inclusive", **kw)  # type: ignore[arg-type]
                vi.alignment.close()
                vi.fasta.close()
                out[name] = {"failed_closed": False, "note": "unexpectedly succeeded"}
            except (Layer1InputError, ContractValidationError, MinosEngineError) as exc:
                out[name] = {"failed_closed": True, "error_type": type(exc).__name__}

        # truncated BAM: build a genuinely large BAM and cut to half (removes BGZF EOF)
        big = build_dataset(
            tmp / "big", simple_reads(200000, n_pairs=4000), contig="chr1", contig_len=200000
        )
        import os as _os

        trunc = tmp / "trunc.bam"
        trunc.write_bytes(big.bam.read_bytes()[: _os.path.getsize(big.bam) // 2])
        import shutil as _sh

        _sh.copy(str(big.bai), str(trunc) + ".bai")
        _expect_fail(
            "truncated_bam",
            bam_path=str(trunc),
            bai_path=str(trunc) + ".bai",
            reference_path=str(big.reference),
            fai_path=str(big.fai),
            region_source="chr1:1-200000",
        )
        # BAM/BAI identity mismatch: the BAI format carries no embedded BAM checksum, so a
        # structurally-valid index built from a different BAM is not detectable by pysam/samtools
        # without an external checksum. Layer 1 records bam+bai content sha256 and the
        # verification_strength (content_hash_and_fetch) as the spec-recommended mitigation.
        out["bam_bai_identity_mismatch"] = {
            "failed_closed": False,
            "documented_limitation": "BAI has no embedded BAM checksum; a valid index of a "
            "different BAM is undetectable without an external checksum. Layer 1 records bam/bai "
            "content sha256 + verification_strength (content_hash_and_fetch) per spec §6.",
        }
        # wrong reference: different contig LENGTH but same name
        wrongref = tmp / "wrongref"
        wrongref.mkdir()
        write_reference(wrongref / "chr1.fa", "chr1", "ACGT" * 500)  # len 2000 != 3000
        _expect_fail(
            "wrong_reference_same_name_len",
            bam_path=str(good.bam),
            bai_path=str(good.bai),
            reference_path=str(wrongref / "chr1.fa"),
            fai_path=str(wrongref / "chr1.fa.fai"),
            region_source="chr1:1-3000",
        )
        # incorrect but readable BAI (corrupt bytes)
        badbai = tmp / "bad.bam.bai"
        badbai.write_bytes(b"not-an-index")
        _expect_fail(
            "incorrect_readable_bai",
            bam_path=str(good.bam),
            bai_path=str(badbai),
            reference_path=str(good.reference),
            fai_path=str(good.fai),
            region_source="chr1:1-3000",
        )

        # wrong reference SAME name + SAME length, different sequence: documented limitation
        samelen = tmp / "samelen"
        samelen.mkdir()
        write_reference(samelen / "chr1.fa", "chr1", "T" * 3000)
        try:
            vi = validate_inputs(
                bam_path=str(good.bam),
                bai_path=str(good.bai),
                reference_path=str(samelen / "chr1.fa"),
                fai_path=str(samelen / "chr1.fa.fai"),
                region_source="chr1:1-3000",
                region_convention="one_based_inclusive",
                adapter=ad,
            )
            vi.alignment.close()
            vi.fasta.close()
            out["wrong_reference_same_len_seq"] = {
                "failed_closed": False,
                "documented_limitation": "same-name same-length different-sequence reference is "
                "not detectable without an @SQ:M5 checksum; validation accepts it (known gap).",
            }
        except MinosEngineError as exc:
            out["wrong_reference_same_len_seq"] = {
                "failed_closed": True,
                "error_type": type(exc).__name__,
            }

    return out
