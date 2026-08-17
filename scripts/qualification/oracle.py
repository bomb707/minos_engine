"""Independent numerical oracle for Layer 1 acceptance (pysam + direct traversal).

IMPORTANT: this module must NOT import any production Layer 1 calculation module
(minos_engine.layer1.scan / coverage / pileup / reference_profile / aggregators /
difficulty). Expected values are computed here from first principles using pysam
fetch, direct CIGAR traversal, an independent difference-array, and independent
statistics — so a match against the production profile is genuine cross-validation.

It reproduces Layer 1's *declared policies* (documented in
reports/LAYER1_FIELD_SEMANTICS.md):
  * observed = reads returned by ``fetch(contig, start0, end0)``;
  * single-reason exclusion priority: unmapped, secondary, supplementary,
    duplicate, qcfail, below_mapq (MAPQ floor 0 -> none excluded);
  * raw alignment counts over ALL observed; MAPQ/BQ/CIGAR over included reads;
  * coverage (duplicate-including view) = +1 over each M/=/X reference block of
    every coverage-eligible read (mapped, non-secondary/supplementary/qcfail);
  * reference context from the FASTA interval sequence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pysam

# CIGAR ops: 0=M 1=I 2=D 3=N 4=S 5=H 6=P 7=(=) 8=X
_ALIGNED = frozenset({0, 7, 8})  # consume ref AND query (depth-contributing)
_REF_ONLY = frozenset({2, 3})  # D, N: consume ref, no depth
_QUERY_CONSUMING = frozenset({0, 1, 4, 7, 8})
_ACGT = frozenset("ACGT")


@dataclass
class OracleResult:
    region: dict[str, Any]
    values: dict[str, Any] = field(default_factory=dict)


def _exclusion_reason(read: pysam.AlignedSegment, min_mapq: int = 0) -> str | None:
    if read.is_unmapped:
        return "unmapped"
    if read.is_secondary:
        return "secondary"
    if read.is_supplementary:
        return "supplementary"
    if read.is_duplicate:
        return "duplicate"
    if read.is_qcfail:
        return "qcfail"
    if read.mapping_quality < min_mapq:
        return "below_mapq"
    return None


def _ref_blocks(read: pysam.AlignedSegment) -> list[tuple[int, int]]:
    """Reference-covered aligned blocks (M/=/X), split at D/N — independent of pysam.get_blocks."""
    blocks: list[tuple[int, int]] = []
    pos = read.reference_start
    for op, length in read.cigartuples or []:
        if op in _ALIGNED:
            blocks.append((pos, pos + length))
            pos += length
        elif op in _REF_ONLY:
            pos += length
    return blocks


def compute(
    bam_path: str,
    bai_path: str,
    reference_path: str,
    contig: str,
    start0: int,
    end0: int,
) -> OracleResult:
    af = pysam.AlignmentFile(bam_path, "rb", index_filename=bai_path)
    fa = pysam.FastaFile(reference_path)
    region_len = end0 - start0

    # raw (over all observed)
    observed = mapped = unmapped = duplicate = secondary = supplementary = 0
    qcfail = paired = proper_pair = reverse = mate_unmapped = 0
    # filter accounting
    excl = dict.fromkeys(
        ("unmapped", "secondary", "supplementary", "duplicate", "qcfail", "below_mapq"), 0
    )
    included = 0
    # included-read stats
    mq_sum = mq0 = mq_lt20 = 0
    aligned_bases = soft = hard = ins = dele = skip = qcons = 0
    softclip_reads = indel_reads = 0
    bases_observed = bases_with_quality = bq_sum = bq_lt20 = 0
    readlens: set[int] = set()
    rl_sum = 0
    # coverage (duplicate-including) via difference array
    diff = np.zeros(region_len + 1, dtype=np.int64)

    for read in af.fetch(contig, start0, end0):
        observed += 1
        if read.is_unmapped:
            unmapped += 1
        else:
            mapped += 1
        if read.is_duplicate:
            duplicate += 1
        if read.is_secondary:
            secondary += 1
        if read.is_supplementary:
            supplementary += 1
        if read.is_qcfail:
            qcfail += 1
        if read.is_paired:
            paired += 1
            if read.mate_is_unmapped:
                mate_unmapped += 1
        if read.is_proper_pair:
            proper_pair += 1
        if (not read.is_unmapped) and read.is_reverse:
            reverse += 1

        cov_eligible = not (
            read.is_unmapped or read.is_secondary or read.is_supplementary or read.is_qcfail
        )
        if cov_eligible:
            for s, e in _ref_blocks(read):
                cs = max(s, start0) - start0
                ce = min(e, end0) - start0
                if ce > cs:
                    diff[cs] += 1
                    diff[ce] -= 1

        reason = _exclusion_reason(read)
        if reason is not None:
            excl[reason] += 1
            continue
        included += 1

        mq = read.mapping_quality
        mq_sum += mq
        if mq == 0:
            mq0 += 1
        if mq < 20:
            mq_lt20 += 1
        qlen = read.query_length or 0
        if qlen:
            rl_sum += qlen
            readlens.add(qlen)
        bases_observed += qlen
        quals = read.query_qualities
        if quals is not None:
            bases_with_quality += len(quals)
            for q in quals:
                bq_sum += q
                if q < 20:
                    bq_lt20 += 1
        for op, length in read.cigartuples or []:
            if op in _ALIGNED:
                aligned_bases += length
            if op in _QUERY_CONSUMING:
                qcons += length
            if op == 4:
                soft += length
            elif op == 5:
                hard += length
            elif op == 1:
                ins += length
            elif op == 2:
                dele += length
            elif op == 3:
                skip += length
        if any(op == 4 for op, _ in (read.cigartuples or [])):
            softclip_reads += 1
        if any(op in (1, 2) for op, _ in (read.cigartuples or [])):
            indel_reads += 1

    af.close()

    depth = np.cumsum(diff[:-1])
    covered = int((depth > 0).sum())
    zero_depth = int((depth == 0).sum())
    sum_depth = int(depth.sum())
    mean_depth = sum_depth / region_len if region_len else 0.0
    max_depth = int(depth.max()) if depth.size else 0

    # reference context (independent)
    seq = fa.fetch(contig, start0, end0).upper()
    fa.close()
    counts = dict.fromkeys("ACGT", 0)
    n_count = 0
    for ch in seq:
        if ch in counts:
            counts[ch] += 1
        else:
            n_count += 1
    acgt = sum(counts.values())
    gc = (counts["G"] + counts["C"]) / acgt if acgt else 0.0
    n_fraction = n_count / len(seq) if seq else 0.0
    entropy = 0.0
    for b in "ACGT":
        if acgt and counts[b]:
            p = counts[b] / acgt
            entropy -= p * math.log2(p)
    # homopolymer runs >= 4
    homo = 0
    i = 0
    while i < len(seq):
        j = i + 1
        while j < len(seq) and seq[j] == seq[i]:
            j += 1
        if seq[i] in _ACGT and (j - i) >= 4:
            homo += j - i
        i = j
    homo_frac = homo / len(seq) if seq else 0.0

    obs = observed or 1
    inc = included or 1
    v: dict[str, Any] = {
        # filter accounting (EXACT)
        "filter.observed": observed,
        "filter.included": included,
        "filter.excluded_unmapped": excl["unmapped"],
        "filter.excluded_secondary": excl["secondary"],
        "filter.excluded_supplementary": excl["supplementary"],
        "filter.excluded_duplicate": excl["duplicate"],
        "filter.excluded_qcfail": excl["qcfail"],
        "filter.excluded_below_mapq": excl["below_mapq"],
        # raw alignment counts (EXACT)
        "reads.total_observed_alignments": observed,
        "reads.mapped": mapped,
        "reads.unmapped": unmapped,
        "reads.duplicate": duplicate,
        "reads.secondary": secondary,
        "reads.supplementary": supplementary,
        "reads.qcfail": qcfail,
        "reads.paired": paired,
        "reads.proper_pair": proper_pair,
        "reads.reverse": reverse,
        "reads.mate_unmapped": mate_unmapped,
        # alignment fractions (float; derived from exact counts)
        "reads.duplicate_fraction": duplicate / obs,
        "reads.secondary_fraction": secondary / obs,
        "reads.supplementary_fraction": supplementary / obs,
        "reads.qcfail_fraction": qcfail / obs,
        "reads.mapped_fraction": mapped / obs,
        "reads.reverse_strand_fraction": reverse / inc,
        # MAPQ (EXACT counts + float mean)
        "mq.count": included,
        "mq.mq0_count": mq0,
        "mq.mq_lt20_count": mq_lt20,
        "mq.mean": mq_sum / inc,
        # base quality (EXACT counts)
        "bq.bases_observed": bases_observed,
        "bq.bases_with_quality": bases_with_quality,
        "bq.bq_lt20_count": bq_lt20,
        "bq.mean": (bq_sum / bases_with_quality) if bases_with_quality else 0.0,
        # read length
        "rl.count": included,
        "rl.mean": rl_sum / inc,
        "rl.variable": len(readlens) > 1,
        # CIGAR (EXACT)
        "cigar.aligned_query_bases": aligned_bases,
        "cigar.soft_clipped_bases": soft,
        "cigar.hard_clipped_bases": hard,
        "cigar.inserted_bases": ins,
        "cigar.deleted_bases": dele,
        "cigar.skipped_bases": skip,
        "cigar.query_consuming_bases": qcons,
        "cigar.softclip_reads": softclip_reads,
        "cigar.indel_reads": indel_reads,
        # coverage (duplicate-including view; EXACT)
        "coverage.region_len": region_len,
        "coverage.covered_bases": covered,
        "coverage.zero_depth_bases": zero_depth,
        "coverage.sum_depth": sum_depth,
        "coverage.mean_depth": mean_depth,
        "coverage.max_depth": max_depth,
        "coverage.zero_depth_fraction": zero_depth / region_len if region_len else 0.0,
        # reference context (float; independent formula)
        "ref.gc_fraction": gc,
        "ref.n_fraction": n_fraction,
        "ref.entropy_bits": entropy,
        "ref.homopolymer_base_fraction": homo_frac,
    }
    return OracleResult(
        region={"contig": contig, "start0": start0, "end0": end0, "length_bp": region_len},
        values=v,
    )
