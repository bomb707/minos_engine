"""Deterministic synthetic Layer 1 fixtures built at runtime with pysam.

No binary BAM/FASTA files are committed — every fixture is generated from these
builders so tests are reproducible and the repository stays free of large genomic
artifacts. Covers the scenarios in Layer 1 spec §18 (sorted/unsorted, missing/
corrupt index, clipping, indels, duplicates, secondary/supplementary, unmapped,
zero-MQ, missing qualities, overlapping mates, GC/N/homopolymer references, tiny
and contig-end regions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pysam

# CIGAR ops: 0=M 1=I 2=D 3=N 4=S 5=H 7=(=) 8=X
FLAG_PAIRED = 1
FLAG_PROPER = 2
FLAG_UNMAPPED = 4
FLAG_MATE_UNMAPPED = 8
FLAG_REVERSE = 16
FLAG_READ1 = 64
FLAG_READ2 = 128
FLAG_SECONDARY = 256
FLAG_QCFAIL = 512
FLAG_DUP = 1024
FLAG_SUPPLEMENTARY = 2048


@dataclass
class ReadSpec:
    name: str
    start0: int
    cigar: list[tuple[int, int]]
    seq: str
    quals: list[int] | None = None
    mapq: int = 60
    is_reverse: bool = False
    is_dup: bool = False
    is_secondary: bool = False
    is_supplementary: bool = False
    is_qcfail: bool = False
    is_unmapped: bool = False
    is_paired: bool = True
    is_proper: bool = True
    is_read1: bool = True
    mate_start0: int | None = None
    tlen: int = 0
    nm: int | None = 0
    md: bool = False


def _flag(spec: ReadSpec) -> int:
    flag = 0
    if spec.is_paired:
        flag |= FLAG_PAIRED
    if spec.is_paired and spec.is_proper:
        flag |= FLAG_PROPER
    if spec.is_unmapped:
        flag |= FLAG_UNMAPPED
    if spec.is_reverse:
        flag |= FLAG_REVERSE
    flag |= FLAG_READ1 if spec.is_read1 else FLAG_READ2
    if spec.is_secondary:
        flag |= FLAG_SECONDARY
    if spec.is_qcfail:
        flag |= FLAG_QCFAIL
    if spec.is_dup:
        flag |= FLAG_DUP
    if spec.is_supplementary:
        flag |= FLAG_SUPPLEMENTARY
    return flag


def make_header(
    contig: str, contig_len: int, sort_order: str = "coordinate"
) -> pysam.AlignmentHeader:
    return pysam.AlignmentHeader.from_dict(
        {
            "HD": {"VN": "1.6", "SO": sort_order},
            "SQ": [{"SN": contig, "LN": contig_len}],
            "RG": [{"ID": "1", "SM": "synthetic", "LB": "lib1", "PL": "ILLUMINA"}],
            "PG": [{"ID": "minos-fixtures", "PN": "minos-fixtures", "VN": "1.0"}],
        }
    )


def _segment(header: pysam.AlignmentHeader, spec: ReadSpec) -> pysam.AlignedSegment:
    a = pysam.AlignedSegment(header)
    a.query_name = spec.name
    a.flag = _flag(spec)
    a.reference_id = 0 if not spec.is_unmapped else -1
    a.reference_start = spec.start0
    a.mapping_quality = spec.mapq
    a.cigartuples = spec.cigar if not spec.is_unmapped else None
    a.query_sequence = spec.seq
    if spec.quals is not None:
        a.query_qualities = pysam.qualitystring_to_array("".join(chr(q + 33) for q in spec.quals))
    if spec.is_paired:
        a.next_reference_id = 0
        a.next_reference_start = spec.mate_start0 if spec.mate_start0 is not None else spec.start0
        a.template_length = spec.tlen
    if spec.nm is not None:
        a.set_tag("NM", spec.nm, "i")
    if spec.md:
        a.set_tag("MD", str(len(spec.seq)), "Z")
    return a


def write_bam(
    path: Path,
    contig: str,
    contig_len: int,
    reads: list[ReadSpec],
    *,
    sort_order: str = "coordinate",
    index: bool = True,
    corrupt_index: bool = False,
) -> tuple[Path, Path | None]:
    """Write a BAM (+ optional index). Returns ``(bam_path, bai_path_or_None)``."""
    header = make_header(contig, contig_len, sort_order)
    # Always write in coordinate order on disk (so the file is indexable); the
    # header's SO label carries the declared sort order under test.
    ordered = sorted(reads, key=lambda r: r.start0)
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for spec in ordered:
            out.write(_segment(header, spec))
    bai: Path | None = None
    if index:
        pysam.index(str(path))
        bai = Path(str(path) + ".bai")
        if corrupt_index:
            bai.write_bytes(b"not a real index")
    return path, bai


def write_reference(path: Path, contig: str, seq: str) -> tuple[Path, Path]:
    """Write a FASTA (wrapped at 60 cols) and its .fai index."""
    lines = [f">{contig}"]
    for i in range(0, len(seq), 60):
        lines.append(seq[i : i + 60])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pysam.faidx(str(path))
    return path, Path(str(path) + ".fai")


# --------------------------------------------------------------------------- #
# Higher-level scenario builder
# --------------------------------------------------------------------------- #
@dataclass
class Dataset:
    bam: Path
    bai: Path
    reference: Path
    fai: Path
    contig: str
    contig_len: int
    region_source: str
    reads: list[ReadSpec] = field(default_factory=list)


def simple_reads(
    contig_len: int,
    *,
    n_pairs: int = 40,
    read_len: int = 100,
    start: int = 100,
    step: int = 20,
    base: str = "A",
) -> list[ReadSpec]:
    """A tidy set of proper FR read pairs across a small span (deterministic)."""
    reads: list[ReadSpec] = []
    seq = base * read_len
    quals = [35] * read_len
    for i in range(n_pairs):
        s1 = start + i * step
        s2 = s1 + 150
        tlen = (s2 + read_len) - s1
        reads.append(
            ReadSpec(
                name=f"p{i}",
                start0=s1,
                cigar=[(0, read_len)],
                seq=seq,
                quals=quals,
                is_read1=True,
                mate_start0=s2,
                tlen=tlen,
                nm=0,
            )
        )
        reads.append(
            ReadSpec(
                name=f"p{i}",
                start0=s2,
                cigar=[(0, read_len)],
                seq=seq,
                quals=quals,
                is_read1=False,
                is_reverse=True,
                mate_start0=s1,
                tlen=-tlen,
                nm=0,
            )
        )
    return reads


def build_dataset(
    tmp: Path,
    reads: list[ReadSpec],
    *,
    contig: str = "chr1",
    contig_len: int = 5000,
    ref_seq: str | None = None,
    region_source: str | None = None,
    sort_order: str = "coordinate",
    index: bool = True,
    corrupt_index: bool = False,
) -> Dataset:
    tmp.mkdir(parents=True, exist_ok=True)
    ref_seq = ref_seq or _default_reference(contig_len)
    ref, fai = write_reference(tmp / f"{contig}.fa", contig, ref_seq)
    bam, bai = write_bam(
        tmp / "input.bam",
        contig,
        contig_len,
        reads,
        sort_order=sort_order,
        index=index,
        corrupt_index=corrupt_index,
    )
    region = region_source or f"{contig}:1-{contig_len}"
    return Dataset(
        bam=bam,
        bai=bai or (tmp / "missing.bai"),
        reference=ref,
        fai=fai,
        contig=contig,
        contig_len=contig_len,
        region_source=region,
        reads=reads,
    )


def _default_reference(length: int) -> str:
    # Deterministic mixed-composition reference with a homopolymer run and GC block.
    unit = "ACGTACGTGCGCATATTTTTTTTAACCGGAACCGG"
    return (unit * (length // len(unit) + 1))[:length]
