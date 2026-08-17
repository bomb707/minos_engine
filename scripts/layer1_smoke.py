#!/usr/bin/env python
"""CI smoke: build a tiny synthetic BAM/FASTA with pysam and profile it.

Self-contained (no test imports) so it runs from a clean CI checkout. Exits
nonzero if Layer 1 profiling does not complete deterministically.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pysam

from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import ProfileRequest, ProfileStatus
from minos_engine.layer1.service import Layer1Service


def _build(tmp: Path) -> tuple[str, str, str, str, str]:
    contig, length = "chr1", 4000
    seq = ("ACGTACGTGCGCATATTTTTTTTAACCGG" * (length // 29 + 1))[:length]
    ref = tmp / "chr1.fa"
    ref.write_text(">chr1\n" + "\n".join(seq[i : i + 60] for i in range(0, len(seq), 60)) + "\n")
    pysam.faidx(str(ref))
    header = pysam.AlignmentHeader.from_dict(
        {
            "HD": {"VN": "1.6", "SO": "coordinate"},
            "SQ": [{"SN": contig, "LN": length}],
            "RG": [{"ID": "1", "SM": "synthetic", "PL": "ILLUMINA"}],
        }
    )
    bam = tmp / "input.bam"
    reads = []
    for i in range(60):
        s1 = 100 + i * 20
        for read1, start, mate, tlen, rev in (
            (True, s1, s1 + 150, 260, False),
            (False, s1 + 150, s1, -260, True),
        ):
            a = pysam.AlignedSegment(header)
            a.query_name = f"p{i}"
            a.flag = 1 | 2 | (64 if read1 else 128) | (16 if rev else 0)
            a.reference_id = 0
            a.reference_start = start
            a.mapping_quality = 60
            a.cigartuples = [(0, 100)]
            a.query_sequence = "A" * 100
            a.query_qualities = pysam.qualitystring_to_array("I" * 100)
            a.next_reference_id = 0
            a.next_reference_start = mate
            a.template_length = tlen
            a.set_tag("NM", 0, "i")
            reads.append(a)
    reads.sort(key=lambda r: r.reference_start)
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for a in reads:
            out.write(a)
    pysam.index(str(bam))
    return str(bam), str(bam) + ".bai", str(ref), str(ref) + ".fai", f"{contig}:1-{length}"


def main() -> int:
    cfg = load_layer1_config()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bam, bai, ref, fai, region = _build(tmp)
        req = ProfileRequest(
            round_id="ci-smoke",
            bam_path=bam,
            bai_path=bai,
            reference_path=ref,
            fai_path=fai,
            region_source=region,
            region_coordinate_convention="one_based_inclusive",
            budget_seconds=120,
            cpu_limit=1,
            memory_limit_bytes=1_000_000_000,
            profiler_config_version=cfg.profiler_config_version,
            profiler_config_hash=cfg.config_hash,
        )
        svc = Layer1Service(require_prerequisite=False)
        out = tmp / "out"
        result = svc.analyze(req, out)
        if result.status is not ProfileStatus.COMPLETE:
            print(f"FAILED: status={result.status.value} warnings={result.warnings}")
            return 1
        b1 = svc.profile(req).fingerprint.fingerprint_hash
        b2 = svc.profile(req).fingerprint.fingerprint_hash
        if b1 != b2:
            print("FAILED: non-deterministic fingerprint")
            return 1
        print(f"OK layer1 profile COMPLETE fingerprint={b1[:16]} artifacts={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
